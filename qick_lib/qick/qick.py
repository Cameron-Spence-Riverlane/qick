"""
The lower-level driver for the QICK library. Contains classes for interfacing with the SoC.
"""
import os
from pynq.overlay import Overlay
import xrfclk
import xrfdc
import numpy as np
import time
import queue
from . import bitfile_path, obtain
from .ip import SocIp, QickMetadata
from .parser import parse_to_bin
from .streamer import DataStreamer
from .qick_asm import QickConfig, QickProgram
from .drivers.generator import *
from .drivers.readout import *
from .drivers.tproc import *


class AxisSwitch(SocIp):
    """
    AxisSwitch class to control Xilinx AXI-Stream switch IP

    :param nslave: Number of slave interfaces
    :type nslave: int
    :param nmaster: Number of master interfaces
    :type nmaster: int
    """
    bindto = ['xilinx.com:ip:axis_switch:1.1']
    REGISTERS = {'ctrl': 0x0, 'mix_mux': 0x040}

    def __init__(self, description):
        """
        Constructor method
        """
        super().__init__(description)

        # Number of slave interfaces.
        self.NSL = int(description['parameters']['NUM_SI'])
        # Number of master interfaces.
        self.NMI = int(description['parameters']['NUM_MI'])

        # Init axis_switch.
        self.ctrl = 0
        self.disable_ports()

    def disable_ports(self):
        """
        Disables ports
        """
        for ii in range(self.NMI):
            offset = self.REGISTERS['mix_mux'] + 4*ii
            self.write(offset, 0x80000000)

    def sel(self, mst=0, slv=0):
        """
        Digitally connects a master interface with a slave interface

        :param mst: Master interface
        :type mst: int
        :param slv: Slave interface
        :type slv: int
        """
        # Sanity check.
        if slv > self.NSL-1:
            print("%s: Slave number %d does not exist in block." %
                  __class__.__name__)
            return
        if mst > self.NMI-1:
            print("%s: Master number %d does not exist in block." %
                  __class__.__name__)
            return

        # Disable register update.
        self.ctrl = 0

        # Disable all MI ports.
        self.disable_ports()

        # MI[mst] -> SI[slv]
        offset = self.REGISTERS['mix_mux'] + 4*mst
        self.write(offset, slv)

        # Enable register update.
        self.ctrl = 2


class RFDC(xrfdc.RFdc):
    """
    Extends the xrfdc driver.
    Since operations on the RFdc tend to be slow (tens of ms), we cache the Nyquist zone and frequency.
    """
    bindto = ["xilinx.com:ip:usp_rf_data_converter:2.3",
              "xilinx.com:ip:usp_rf_data_converter:2.4",
              "xilinx.com:ip:usp_rf_data_converter:2.6"]

    def __init__(self, description):
        """
        Constructor method
        """
        super().__init__(description)
        # Nyquist zone for each channel
        self.nqz_dict = {}
        # Rounded NCO frequency for each channel
        self.mixer_dict = {}

    def configure(self, soc):
        self.daccfg = soc.dacs
        self.adccfg = soc.adcs

    def set_mixer_freq(self, dacname, f, force=False, reset=False):
        """
        Set the NCO frequency that will be mixed with the generator output.

        The RFdc driver does its own math to convert a frequency to a register value.
        (see XRFdc_SetMixerSettings in xrfdc_mixer.c, and "NCO Frequency Conversion" in PG269)
        This is what it does:
        1. Add/subtract fs to get the frequency in the range of [-fs/2, fs/2].
        2. If the original frequency was not in [-fs/2, fs/2] and the DAC is configured for 2nd Nyquist zone, multiply by -1.
        3. Convert to a 48-bit register value, rounding using C integer casting (i.e. round towards 0).

        Step 2 is not desirable for us, so we must undo it.

        The rounding gives unexpected results sometimes: it's hard to tell if a freq will get rounded up or down.
        This is important if the demanded frequency was rounded to a valid frequency for frequency matching.
        The safest way to get consistent behavior is to always round to a valid NCO frequency.
        We are trusting that the floating-point math is exact and a number we rounded here is still a round number in the RFdc driver.

        :param dacname: DAC channel (2-digit string)
        :type dacname: int
        :param f: NCO frequency
        :type f: float
        :param force: force update, even if the setting is the same
        :type force: bool
        :param reset: if we change the frequency, also reset the NCO's phase accumulator
        :type reset: bool
        """
        fs = self.daccfg[dacname]['fs']
        fstep = fs/2**48
        rounded_f = round(f/fstep)*fstep
        if not force and rounded_f == self.get_mixer_freq(dacname):
            return
        fset = rounded_f
        if abs(rounded_f) > fs/2 and self.get_nyquist(dacname)==2:
            fset *= -1

        tile, channel = [int(a) for a in dacname]
        # Make a copy of mixer settings.
        dac_mixer = self.dac_tiles[tile].blocks[channel].MixerSettings
        new_mixcfg = dac_mixer.copy()

        # Update the copy
        new_mixcfg.update({
            'EventSource': xrfdc.EVNT_SRC_IMMEDIATE,
            'Freq': fset,
            'MixerType': xrfdc.MIXER_TYPE_FINE,
            'PhaseOffset': 0})

        # Update settings.
        if reset: self.dac_tiles[tile].blocks[channel].ResetNCOPhase()
        self.dac_tiles[tile].blocks[channel].MixerSettings = new_mixcfg
        self.dac_tiles[tile].blocks[channel].UpdateEvent(xrfdc.EVENT_MIXER)
        self.mixer_dict[dacname] = rounded_f

    def get_mixer_freq(self, dacname):
        try:
            return self.mixer_dict[dacname]
        except KeyError:
            tile, channel = [int(a) for a in dacname]
            self.mixer_dict[dacname] = self.dac_tiles[tile].blocks[channel].MixerSettings['Freq']
            return self.mixer_dict[dacname]

    def set_nyquist(self, dacname, nqz, force=False):
        """
        Sets DAC channel to operate in Nyquist zone nqz.
        This setting doesn't change the output frequencies:
        you will always have some power at both the demanded frequency and its image(s).
        Setting the NQZ to 2 increases output power in the 2nd/3rd Nyquist zones.
        See "RF-DAC Nyquist Zone Operation" in PG269.

        :param dacname: DAC channel (2-digit string)
        :type dacname: int
        :param nqz: Nyquist zone (1 or 2)
        :type nqz: int
        :param force: force update, even if the setting is the same
        :type force: bool
        """
        if nqz not in [1,2]:
            raise RuntimeError("Nyquist zone must be 1 or 2")
        tile, channel = [int(a) for a in dacname]
        if not force and self.get_nyquist(dacname) == nqz:
            return
        self.dac_tiles[tile].blocks[channel].NyquistZone = nqz
        self.nqz_dict[dacname] = nqz

    def get_nyquist(self, dacname):
        try:
            return self.nqz_dict[dacname]
        except KeyError:
            tile, channel = [int(a) for a in dacname]
            self.nqz_dict[dacname] = self.dac_tiles[tile].blocks[channel].NyquistZone
            return self.nqz_dict[dacname]


class QickSoc(Overlay, QickConfig):
    """
    QickSoc class. This class will create all object to access system blocks

    :param bitfile: Path to the bitfile. This should end with .bit, and the corresponding .hwh file must be in the same directory.
    :type bitfile: str
    :param force_init_clks: Re-initialize the board clocks regardless of whether they appear to be locked. Specifying (as True or False) the clk_output or external_clk options will also force clock initialization.
    :type force_init_clks: bool
    :param clk_output: If true, output a copy of the RF reference. This option is supported for the ZCU111 (get 122.88 MHz from J108) and ZCU216 (get 245.76 MHz from OUTPUT_REF J10).
    :type clk_output: bool or None
    :param external_clk: If true, lock the board clocks to an external reference. This option is supported for the ZCU111 (put 12.8 MHz on External_REF_CLK J109), ZCU216 (put 10 MHz on INPUT_REF_CLK J11), and RFSoC 4x2 (put 10 MHz on CLK_IN).
    :type external_clk: bool or None
    :param ignore_version: Whether version discrepancies between PYNQ build and firmware build are ignored
    :type ignore_version: bool
    """

    # The following constants are no longer used. Some of the values may not match the bitfile.
    # fs_adc = 384*8 # MHz
    # fs_dac = 384*16 # MHz
    # pulse_mem_len_IQ = 65536 # samples for I, Q
    # ADC_decim_buf_len_IQ = 1024 # samples for I, Q
    # ADC_accum_buf_len_IQ = 16384 # samples for I, Q
    #tProc_instruction_len_bytes = 8
    #tProc_prog_mem_samples = 8000
    #tProc_prog_mem_size_bytes_tot = tProc_instruction_len_bytes*tProc_prog_mem_samples
    #tProc_data_len_bytes = 4
    #tProc_data_mem_samples = 4096
    #tProc_data_mem_size_bytes_tot = tProc_data_len_bytes*tProc_data_mem_samples
    #tProc_stack_len_bytes = 4
    #tProc_stack_samples = 256
    #tProc_stack_size_bytes_tot = tProc_stack_len_bytes*tProc_stack_samples
    #phase_resolution_bits = 32
    #gain_resolution_signed_bits = 16

    # Constructor.
    def __init__(self, bitfile=None, force_init_clks=False, ignore_version=True, no_tproc=False, clk_output=None, external_clk=None, **kwargs):
        """
        Constructor method
        """

        self.external_clk = external_clk
        self.clk_output = clk_output
        # Load bitstream. We read the bitstream configuration from the HWH file, but we don't program the FPGA yet.
        # We need to program the clocks first.
        if bitfile is None:
            Overlay.__init__(self, bitfile_path(
            ), ignore_version=ignore_version, download=False, **kwargs)
        else:
            Overlay.__init__(
                self, bitfile, ignore_version=ignore_version, download=False, **kwargs)

        # Initialize the configuration
        self._cfg = {}
        QickConfig.__init__(self)

        self['board'] = os.environ["BOARD"]

        # Read the config to get a list of enabled ADCs and DACs, and the sampling frequencies.
        self.list_rf_blocks(
            self.ip_dict['usp_rf_data_converter_0']['parameters'])

        self.config_clocks(force_init_clks)

        # RF data converter (for configuring ADCs and DACs, and setting NCOs)
        self.rf = self.usp_rf_data_converter_0
        self.rf.configure(self)

        # Extract the IP connectivity information from the HWH parser and metadata.
        self.metadata = QickMetadata(self)

        if not no_tproc:
            # tProcessor, 64-bit instruction, 32-bit registers, x8 channels.
            if 'axis_tproc64x32_x8_0' in self.ip_dict:
                self._tproc = self.axis_tproc64x32_x8_0
                self._tproc.configure(self.axi_bram_ctrl_0, self.axi_dma_tproc)
                self['fs_proc'] = self.metadata.get_fclk(self.tproc.fullpath, "aclk")
            else:
                self._tproc = self.axis_tproc_v2_0
                self._tproc.configure(self.axi_dma_tproc)
                self['fs_proc'] = self.metadata.get_fclk(self.tproc.fullpath, "t_clk_i")

            self.map_signal_paths()

            self._streamer = DataStreamer(self)

            # list of objects that need to be registered for autoproxying over Pyro
            self.autoproxy = [self.streamer, self.tproc]

    @property
    def tproc(self):
        return self._tproc

    @property
    def streamer(self):
        return self._streamer

    def map_signal_paths(self):
        """
        Make lists of signal generator, readout, and buffer blocks in the firmware.
        Also map the switches connecting the generators and buffers to DMA.
        Fill the config dictionary with parameters of the DAC and ADC channels.
        """
        # Use the HWH parser to trace connectivity and deduce the channel numbering.
        for key, val in self.ip_dict.items():
            if hasattr(val['driver'], 'configure_connections'):
                getattr(self, key).configure_connections(self)

        # Signal generators (anything driven by the tProc)
        self.gens = []

        # Constant generators
        self.iqs = []

        # Average + Buffer blocks.
        self.avg_bufs = []

        # Readout blocks.
        self.readouts = []
        ro_drivers = set([AxisReadoutV2, AxisPFBReadoutV2])

        # Populate the lists with the registered IP blocks.
        for key, val in self.ip_dict.items():
            if issubclass(val['driver'], AbsPulsedSignalGen):
                self.gens.append(getattr(self, key))
            elif val['driver'] == AxisConstantIQ:
                self.iqs.append(getattr(self, key))
            elif val['driver'] in ro_drivers:
                self.readouts.append(getattr(self, key))
            elif val['driver'] == AxisAvgBuffer:
                self.avg_bufs.append(getattr(self, key))

        # AxisReadoutV3 isn't a PYNQ-registered IP block, so we add it here
        for buf in self.avg_bufs:
            if buf.readout not in self.readouts:
                self.readouts.append(buf.readout)

        # Sort the lists.
        # We order gens by the tProc port number and buffers by the switch port number.
        # Those orderings are important, since those indices get used in programs.
        self.gens.sort(key=lambda x: x['tproc_ch'])
        self.avg_bufs.sort(key=lambda x: x.switch_ch)
        # The IQ and readout orderings aren't critical for anything.
        self.iqs.sort(key=lambda x: x.dac)
        self.readouts.sort(key=lambda x: x.adc)

        # which generators have waveform memories?
        arb_gens = filter(lambda x: isinstance(x, AbsArbSignalGen), self.gens)
        # Configure the DMA connections to upload waveforms to generators.
        if arb_gens:
            # AXIS Switch to upload samples into Signal Generators.
            self.switch_gen = self.axis_switch_gen

            """
            # This sanity check doesn't always pass, we have firmwares that don't use all the switch ports.
            if self.switch_gen.NMI != len(arb_gens):
                raise RuntimeError("We have %d switch_gen outputs but %d arbitrary-waveform generator blocks." %
                                   (self.switch_gen.NMI, len(arb_gens)))
            """

            for gen in arb_gens:
                gen.configure_dma(self.axi_dma_gen, self.switch_gen)

        # Configure the DMA connections to download data from avg+buffer blocks.
        if self.avg_bufs:
            # AXIS Switch to read samples from averager.
            self.switch_avg = self.axis_switch_avg
            # AXIS Switch to read samples from buffer.
            self.switch_buf = self.axis_switch_buf
            # Sanity check: we should have the same number of buffer blocks as switch ports.
            if self.switch_avg.NSL != len(self.avg_bufs):
                raise RuntimeError("We have %d switch_avg inputs but %d avg/buffer blocks." %
                                   (self.switch_avg.NSL, len(self.avg_bufs)))
            if self.switch_buf.NSL != len(self.avg_bufs):
                raise RuntimeError("We have %d switch_buf inputs but %d avg/buffer blocks." %
                                   (self.switch_buf.NSL, len(self.avg_bufs)))

            for buf in self.avg_bufs:
                buf.configure(self.axi_dma_avg, self.switch_avg,
                              self.axi_dma_buf, self.switch_buf)

        # Configure the drivers.
        for i, gen in enumerate(self.gens):
            gen.configure(i, self.rf, self.dacs[gen.dac]['fs'])

        for i, iq in enumerate(self.iqs):
            iq.configure(i, self.rf, self.dacs[iq.dac]['fs'])

        for readout in self.readouts:
            readout.configure(self.rf, self.adcs[readout.adc]['fs'])

        # Fill the config dictionary with driver parameters.
        self['dacs'] = list(self.dacs.keys())
        self['adcs'] = list(self.adcs.keys())
        self['gens'] = [gen.cfg for gen in self.gens]
        self['iqs'] = [iq.cfg for iq in self.iqs]

        # In the config, we define a "readout" as the chain of ADC+readout+buffer.
        def merge_cfgs(bufcfg, rocfg):
            merged = {**bufcfg, **rocfg}
            for k in set(bufcfg.keys()) & set(rocfg.keys()):
                del merged[k]
                merged["avgbuf_"+k] = bufcfg[k]
                merged["ro_"+k] = rocfg[k]
            return merged
        self['readouts'] = [merge_cfgs(buf.cfg, buf.readout.cfg) for buf in self.avg_bufs]

        self['tprocs'] = [self.tproc.cfg]

    def config_clocks(self, force_init_clks):
        """
        Configure PLLs if requested, or if any ADC/DAC is not locked.
        """
              
        # if we're changing the clock config, we must set the clocks to apply the config
        if force_init_clks or (self.external_clk is not None) or (self.clk_output is not None):
            self.set_all_clks()
            self.download()
        else:
            self.download()
            if not self.clocks_locked():
                self.set_all_clks()
                self.download()
        if not self.clocks_locked():
            print(
                "Not all DAC and ADC PLLs are locked. You may want to repeat the initialization of the QickSoc.")

    def clocks_locked(self):
        """
        Checks whether the DAC and ADC PLLs are locked.
        This can only be run after the bitstream has been downloaded.

        :return: clock status
        :rtype: bool
        """

        dac_locked = [self.usp_rf_data_converter_0.dac_tiles[iTile]
                      .PLLLockStatus == 2 for iTile in self.dac_tiles]
        adc_locked = [self.usp_rf_data_converter_0.adc_tiles[iTile]
                      .PLLLockStatus == 2 for iTile in self.adc_tiles]
        return all(dac_locked) and all(adc_locked)

    def list_rf_blocks(self, rf_config):
        """
        Lists the enabled ADCs and DACs and get the sampling frequencies.
        XRFdc_CheckBlockEnabled in xrfdc_ap.c is not accessible from the Python interface to the XRFdc driver.
        This re-implements that functionality.
        """

        self.hs_adc = rf_config['C_High_Speed_ADC'] == '1'

        self.dac_tiles = []
        self.adc_tiles = []
        dac_fabric_freqs = []
        adc_fabric_freqs = []
        refclk_freqs = []
        self.dacs = {}
        self.adcs = {}

        for iTile in range(4):
            if rf_config['C_DAC%d_Enable' % (iTile)] != '1':
                continue
            self.dac_tiles.append(iTile)
            f_fabric = float(rf_config['C_DAC%d_Fabric_Freq' % (iTile)])
            f_refclk = float(rf_config['C_DAC%d_Refclk_Freq' % (iTile)])
            dac_fabric_freqs.append(f_fabric)
            refclk_freqs.append(f_refclk)
            fs = float(rf_config['C_DAC%d_Sampling_Rate' % (iTile)])*1000
            for iBlock in range(4):
                if rf_config['C_DAC_Slice%d%d_Enable' % (iTile, iBlock)] != 'true':
                    continue
                self.dacs["%d%d" % (iTile, iBlock)] = {'fs': fs,
                                                       'f_fabric': f_fabric}

        for iTile in range(4):
            if rf_config['C_ADC%d_Enable' % (iTile)] != '1':
                continue
            self.adc_tiles.append(iTile)
            f_fabric = float(rf_config['C_ADC%d_Fabric_Freq' % (iTile)])
            f_refclk = float(rf_config['C_ADC%d_Refclk_Freq' % (iTile)])
            adc_fabric_freqs.append(f_fabric)
            refclk_freqs.append(f_refclk)
            fs = float(rf_config['C_ADC%d_Sampling_Rate' % (iTile)])*1000
            for iBlock in range(4):
                if self.hs_adc:
                    if iBlock >= 2 or rf_config['C_ADC_Slice%d%d_Enable' % (iTile, 2*iBlock)] != 'true':
                        continue
                else:
                    if rf_config['C_ADC_Slice%d%d_Enable' % (iTile, iBlock)] != 'true':
                        continue
                self.adcs["%d%d" % (iTile, iBlock)] = {'fs': fs,
                                                       'f_fabric': f_fabric}

        def get_common_freq(freqs):
            """
            Check that all elements of the list are equal, and return the common value.
            """
            if not freqs:  # input is empty list
                return None
            if len(set(freqs)) != 1:
                raise RuntimeError("Unexpected frequencies:", freqs)
            return freqs[0]

        self['refclk_freq'] = get_common_freq(refclk_freqs)

    def set_all_clks(self):
        """
        Resets all the board clocks
        """
        if self['board'] == 'ZCU111':
            print("resetting clocks:", self['refclk_freq'])

            if hasattr(xrfclk, "xrfclk"): # pynq 2.7
                # load the default clock chip configurations from file, so we can then modify them
                xrfclk.xrfclk._find_devices()
                xrfclk.xrfclk._read_tics_output()
                if self.clk_output:
                    # change the register for the LMK04208 chip's 5th output, which goes to J108
                    # we need this for driving the RF board
                    xrfclk.xrfclk._Config['lmk04208'][122.88][6] = 0x00140325
                if self.external_clk:
                    # default value is 0x2302886D
                    xrfclk.xrfclk._Config['lmk04208'][122.88][14] = 0x2302826D
            else: # pynq 2.6
                if self.clk_output:
                    # change the register for the LMK04208 chip's 5th output, which goes to J108
                    # we need this for driving the RF board
                    xrfclk._lmk04208Config[122.88][6] = 0x00140325
                else: # restore the default
                    xrfclk._lmk04208Config[122.88][6] = 0x80141E05
                if self.external_clk:
                    xrfclk._lmk04208Config[122.88][14] = 0x2302826D
                else: # restore the default
                    xrfclk._lmk04208Config[122.88][14] = 0x2302886D
            xrfclk.set_all_ref_clks(self['refclk_freq'])
        elif self['board'] == 'ZCU216':
            lmk_freq = self['refclk_freq']
            lmx_freq = self['refclk_freq']*2
            print("resetting clocks:", lmk_freq, lmx_freq)

            assert hasattr(xrfclk, "xrfclk") # ZCU216 only has a pynq 2.7 image
            xrfclk.xrfclk._find_devices()
            xrfclk.xrfclk._read_tics_output()
            if self.external_clk:
                # default value is 0x01471A
                xrfclk.xrfclk._Config['lmk04828'][245.76][80] = 0x01470A
            if self.clk_output:
                # default value is 0x012C22
                xrfclk.xrfclk._Config['lmk04828'][245.76][55] = 0x012C02
            xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)
        elif self['board'] == 'RFSoC4x2':
            lmk_freq = self['refclk_freq']/2
            lmx_freq = self['refclk_freq']
            print("resetting clocks:", lmk_freq, lmx_freq)
            xrfclk.xrfclk._find_devices()
            xrfclk.xrfclk._read_tics_output()
            print(xrfclk.xrfclk._Config['lmk04828'][245.76][80])
            if self.external_clk:
                # default value is 0x01471A
                xrfclk.xrfclk._Config['lmk04828'][245.76][80] = 0x01470A
            xrfclk.set_ref_clks(lmk_freq=lmk_freq, lmx_freq=lmx_freq)

    def get_decimated(self, ch, address=0, length=None):
        """
        Acquires data from the readout decimated buffer

        :param ch: ADC channel
        :type ch: int
        :param address: Address of data
        :type address: int
        :param length: Buffer transfer length
        :type length: int
        :return: List of I and Q decimated arrays
        :rtype: list
        """
        if length is None:
            # this default will always cause a RuntimeError
            # TODO: remove the default, or pick a better fallback value
            length = self.avg_bufs[ch].BUF_MAX_LENGTH

        # we must transfer an even number of samples, so we pad the transfer size
        transfer_len = length + length % 2

        # there is a bug which causes the first sample of a transfer to always be the sample at address 0
        # we work around this by requesting an extra 2 samples at the beginning
        data = self.avg_bufs[ch].transfer_buf(
            (address-2) % self.avg_bufs[ch].BUF_MAX_LENGTH, transfer_len+2)

        # we remove the padding here
        return data[2:length+2]

    def get_accumulated(self, ch, address=0, length=None):
        """
        Acquires data from the readout accumulated buffer

        :param ch: ADC channel
        :type ch: int
        :param address: Address of data
        :type address: int
        :param length: Buffer transfer length
        :type length: int
        :returns:
            - di[:length] (:py:class:`list`) - list of accumulated I data
            - dq[:length] (:py:class:`list`) - list of accumulated Q data
        """
        if length is None:
            # this default will always cause a RuntimeError
            # TODO: remove the default, or pick a better fallback value
            length = self.avg_bufs[ch].AVG_MAX_LENGTH

        # we must transfer an even number of samples, so we pad the transfer size
        transfer_len = length + length % 2

        # there is a bug which causes the first sample of a transfer to always be the sample at address 0
        # we work around this by requesting an extra 2 samples at the beginning
        data = self.avg_bufs[ch].transfer_avg(
            (address-2) % self.avg_bufs[ch].AVG_MAX_LENGTH, transfer_len+2)

        # we remove the padding here
        return data[2:length+2]

    def init_readouts(self):
        """
        Initialize readouts, in preparation for configuring them.
        """
        for readout in self.readouts:
            # if this is a tProc-controlled readout, we don't initialize it here
            if not isinstance(readout, AxisReadoutV3):
                readout.initialize()

    def configure_readout(self, ch, output, frequency, gen_ch=0):
        """Configure readout channel output style and frequency.
        This method is only for use with PYNQ-controlled readouts.
        :param ch: Channel to configure
        :type ch: int
        :param output: output type from 'product', 'dds', 'input'
        :type output: str
        :param frequency: frequency
        :type frequency: float
        :param gen_ch: DAC channel (use None if you don't want to round to a valid DAC frequency)
        :type gen_ch: int
        """
        buf = self.avg_bufs[ch]
        buf.readout.set_out(sel=output)
        buf.set_freq(frequency, gen_ch=gen_ch)

    def config_avg(self, ch, address=0, length=1, enable=True):
        """Configure and optionally enable accumulation buffer
        :param ch: Channel to configure
        :type ch: int
        :param address: Starting address of buffer
        :type address: int
        :param length: length of buffer (how many samples to take)
        :type length: int
        :param enable: True to enable buffer
        :type enable: bool
        """
        avg_buf = self.avg_bufs[ch]
        avg_buf.config_avg(address, length)
        if enable:
            avg_buf.enable_avg()

    def config_buf(self, ch, address=0, length=1, enable=True):
        """Configure and optionally enable decimation buffer
        :param ch: Channel to configure
        :type ch: int
        :param address: Starting address of buffer
        :type address: int
        :param length: length of buffer (how many samples to take)
        :type length: int
        :param enable: True to enable buffer
        :type enable: bool
        """
        avg_buf = self.avg_bufs[ch]
        avg_buf.config_buf(address, length)
        if enable:
            avg_buf.enable_buf()

    def get_avg_max_length(self, ch=0):
        """Get accumulation buffer length for channel
        :param ch: Channel
        :type ch: int
        :return: Length of accumulation buffer for channel 'ch'
        :rtype: int
        """
        return self['readouts'][ch]['avg_maxlen']

    def load_pulse_data(self, ch, data, addr):
        """Load pulse data into signal generators
        :param ch: Channel
        :type ch: int
        :param data: array of (I, Q) values for pulse envelope
        :type data: int16 array
        :param addr: address to start data at
        :type addr: int
        """
        return self.gens[ch].load(xin=data, addr=addr)

    def set_nyquist(self, ch, nqz, force=False):
        """
        Sets DAC channel ch to operate in Nyquist zone nqz mode.

        :param ch: DAC channel (index in 'gens' list)
        :type ch: int
        :param nqz: Nyquist zone
        :type nqz: int
        """

        self.gens[ch].set_nyquist(nqz)

    def set_mixer_freq(self, ch, f, ro_ch=None):
        """
        Set mixer frequency for a signal generator.
        If the generator does not have a mixer, you will get an error.

        :param ch: DAC channel (index in 'gens' list)
        :type ch: int
        :param f: frequency (MHz)
        :type f: float
        :param ro_ch: readout channel (use None if you don't want to round to a valid ADC frequency)
        :type ro_ch: int
        """
        if self.gens[ch].HAS_MIXER:
            self.gens[ch].set_mixer_freq(f, ro_ch)
        elif f != 0:
            raise RuntimeError("tried to set a mixer frequency, but this channel doesn't have a mixer")

    def set_mux_freqs(self, ch, freqs, gains=None, ro_ch=0):
        """
        Set muxed frequencies and gains for a signal generator.
        If it's not a muxed signal generator, you will get an error.

        Gains can only be specified for a muxed generator with configurable gains.
        The gain list must be the same length as the freqs list.

        :param ch: DAC channel (index in 'gens' list)
        :type ch: int
        :param freqs: frequencies (MHz)
        :type freqs: list
        :param gains: gains (in range -1 to 1)
        :type gains: list
        :param ro_ch: readout channel (use None if you don't want to round to a valid ADC frequency)
        :type ro_ch: int
        """
        if gains is not None and len(gains) != len(freqs):
            raise RuntimeError("lengths of freqs and gains lists do not match")
        for ii, f in enumerate(freqs):
            self.gens[ch].set_freq(f, out=ii, ro_ch=ro_ch)
            if gains is not None:
                self.gens[ch].set_gain(gains[ii], out=ii)

    def set_iq(self, ch, f, i, q):
        """
        Set frequency, I, and Q for a constant-IQ output.

        :param ch: IQ channel (index in 'iqs' list)
        :type ch: int
        :param f: frequency (MHz)
        :type f: float
        :param i: I value (in range -1 to 1)
        :type i: float
        :param q: Q value (in range -1 to 1)
        :type q: float
        """
        self.iqs[ch].set_mixer_freq(f)
        self.iqs[ch].set_iq(i, q)

    def load_bin_program(self, binprog, reset=False):
        """
        Write the program to the tProc program memory.

        :param reset: Reset the tProc before writing the program.
        :type reset: bool
        """
        if reset: self.tproc.reset()

        # cast the program words to 64-bit uints
        self.binprog = np.array(obtain(binprog), dtype=np.uint64)
        # reshape to 32 bits to match the program memory
        self.binprog = np.frombuffer(self.binprog, np.uint32)

        self.reload_program()

    def reload_program(self):
        """
        Write the most recently written program to the tProc program memory.
        This is normally useful after a reset (which erases the program memory)
        """
        # write the program to memory with a fast copy
        #print(self.binprog)
        np.copyto(self.tproc.mem.mmio.array[:len(self.binprog)], self.binprog)

    def start_src(self, src):
        """
        Sets the start source of tProc

        :param src: start source "internal" or "external"
        :type src: string
        """
        # set internal-start register to "init"
        # otherwise we might start the tProc on a transition from external to internal start
        self.tproc.start_reg = 0
        self.tproc.start_src_reg = {"internal": 0, "external": 1}[src]

    def reset_gens(self):
        """
        Reset the tProc and run a minimal tProc program that drives all signal generators with 0's.
        Useful for stopping any periodic or stdysel="last" outputs that may have been driven by a previous program.
        """
        prog = QickProgram(self)
        for gen in self.gens:
            if isinstance(gen, AbsArbSignalGen):
                prog.set_pulse_registers(ch=gen.ch, style="const", mode="oneshot", freq=0, phase=0, gain=0, length=3)
                prog.pulse(ch=gen.ch,t=0)
        prog.end()
        # this should always run with internal trigger
        self.start_src("internal")
        prog.load_program(self, reset=True)
        self.tproc.start()

    def start_readout(self, total_reps, counter_addr=1, ch_list=None, reads_per_rep=1, stride=None):
        """
        Start a streaming readout of the accumulated buffers.

        :param total_count: Number of data points expected
        :type total_count: int
        :param counter_addr: Data memory address for the loop counter
        :type counter_addr: int
        :param ch_list: List of readout channels
        :type ch_list: list
        :param reads_per_count: Number of data points to expect per counter increment
        :type reads_per_count: int
        :param stride: Default number of measurements to transfer at a time.
        :type stride: int
        """
        ch_list = obtain(ch_list)
        if ch_list is None: ch_list = [0, 1]
        streamer = self.streamer

        if not streamer.readout_worker.is_alive():
            print("restarting readout worker")
            streamer.start_worker()
            print("worker restarted")

        # if there's still a readout job running, stop it
        if streamer.readout_running():
            print("cleaning up previous readout: stopping tProc and streamer loop")
            # stop the tProc
            self.tproc.reset()
            # tell the readout to stop (this will break the readout loop)
            streamer.stop_readout()
            streamer.done_flag.wait()
            # push a dummy packet into the data queue to halt any running poll_data(), and wait long enough for the packet to be read out
            streamer.data_queue.put((0, None))
            time.sleep(0.1)
            # reload the program (since the reset will have wiped it out)
            self.reload_program()
            print("streamer stopped")
        streamer.stop_flag.clear()

        if streamer.data_available():
            # flush all the data in the streamer buffer
            print("clearing streamer buffer")
            # read until the queue times out, discard the data
            self.poll_data(totaltime=-1, timeout=0.1)
            print("buffer cleared")

        streamer.total_count = total_reps*reads_per_rep
        streamer.count = 0

        streamer.done_flag.clear()
        streamer.job_queue.put((total_reps, counter_addr, ch_list, reads_per_rep, stride))

    def poll_data(self, totaltime=0.1, timeout=None):
        """
        Get as much data as possible from the streamer data queue.
        Stop when any of the following conditions are met:
        * all the data has been transferred (based on the total_count)
        * we got data, and it has been totaltime seconds since poll_data was called
        * timeout is defined, and the timeout expired without getting new data in the queue
        If there are errors in the error queue, raise the first one.

        :param totaltime: How long to acquire data (negative value = ignore total time and total count, just read until timeout)
        :type totaltime: float
        :param timeout: How long to wait for the next data packet (None = wait forever)
        :type timeout: float
        :return: list of (data, stats) pairs, oldest first
        :rtype: list
        """
        streamer = self.streamer

        time_end = time.time() + totaltime
        new_data = []
        while (totaltime < 0) or (streamer.count < streamer.total_count and time.time() < time_end):
            try:
                raise RuntimeError("exception in readout loop") from streamer.error_queue.get(block=False)
            except queue.Empty:
                pass
            try:
                length, data = streamer.data_queue.get(block=True, timeout=timeout)
                # if we stopped the readout while we were waiting for data, break out and return
                if streamer.stop_flag.is_set() or data is None:
                    break
                streamer.count += length
                new_data.append(data)
            except queue.Empty:
                break
        return new_data
