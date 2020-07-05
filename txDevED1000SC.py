#!/usr/bin/python3
"""
Telex Device - ED1000 Communication over Sound Card - Transmit Only

Articles:
https://www.allaboutcircuits.com/technical-articles/fsk-explained-with-python/
https://dsp.stackexchange.com/questions/29946/demodulating-fsk-audio-in-python
https://stackoverflow.com/questions/35759353/demodulating-an-fsk-signal-in-python#

"""
__author__      = "Jochen Krapf"
__email__       = "jk@nerd2nerd.org"
__copyright__   = "Copyright 2018, JK"
__license__     = "GPL3"
__version__     = "0.0.1"

from threading import Thread, Event
import time
import pyaudio
import math
import struct
#import scipy.signal.signaltools as sigtool
from scipy import signal
import numpy as np

import logging
l = logging.getLogger("piTelex." + __name__)

import txCode
import txBase

sample_f = 48000       # sampling rate, Hz, must be integer

# Set to plot receive filters' spectra
plot_spectrum = False

#######

class TelexED1000SC(txBase.TelexBase):
    def __init__(self, **params):
        super().__init__()

        self._mc = txCode.BaudotMurrayCode(loop_back=False)

        self.id = '='
        self.params = params

        self._tx_buffer = []
        self._rx_buffer = []
        self._is_online = Event()
        self._ST_pressed = False
        # Delay going offline by a certain time (see thread_tx) so that operator
        # can read the latest text -- crucial in case of dial errors.
        self.delay_offline = False

        self.recv_squelch = self.params.get('recv_squelch', 100)
        self.recv_debug = self.params.get('recv_debug', False)

        recv_f0 = self.params.get('recv_f0', 2250)
        recv_f1 = self.params.get('recv_f1', 3150)
        recv_f = [recv_f0, recv_f1]
        self._recv_decode_init(recv_f)

        self.run = True
        self._tx_thread = Thread(target=self.thread_tx, name='ED1000tx')
        self._tx_thread.start()
        self._rx_thread = Thread(target=self.thread_rx, name='ED1000rx')
        self._rx_thread.start()



    def __del__(self):
        self.run = False

        super().__del__()


    def exit(self):
        self._run = False

    # =====

    def read(self) -> str:
        if self._rx_buffer:
            a = self._rx_buffer.pop(0)
            l.debug("read: {!r}".format(a))
            return a

    # -----

    def write(self, a:str, source:str):
        l.debug("write from {!r}: {!r}".format(source, a))
        if len(a) != 1:
            self._check_commands(a)
            return
            
        if a == '#':
            a = '@'   # ask teletype for hardware ID

        if a and self._is_online.is_set():
            self._tx_buffer.append(a)

    # =====

    def _check_commands(self, a:str):
        if a == '\x1bA':
            l.debug("going online")
            self._tx_buffer = []
            self._tx_buffer.append('§A')   # signaling type A - connection
            self._set_online(True)

        if a == '\x1bZ':
            l.debug("going offline (ST pressed: {})".format(self._ST_pressed))
            # When going offline, *don't* empty the tx buffer so the tx thread
            # can write out the rest. Critical for ASCII services that send
            # faster than 50 Bd.
            self._set_online(False)
            # ...except if we ourselves initiated going offline (by pressing
            # ST). In this case, also cancel offline delay in thread_tx.
            if self._ST_pressed:
                self._ST_pressed = False
                self.delay_offline = False
                self._tx_buffer = []

        if a == '\x1bWB':
            l.debug("ready to dial")
            self._tx_buffer = []
            self._tx_buffer.append('§W')   # signaling type W - ready for dial
            self._set_online(True)

    # -----

    def _set_online(self, online:bool):
        if online:
            l.debug("set online")
            self._is_online.set()
            # Enable offline delay in case of errors (see thread_tx)
            self.delay_offline = True
        else:
            l.debug("set offline")
            self._is_online.clear()

    # =====

    def thread_tx(self):
        """Handler for sending tones."""

        devindex = self.params.get('devindex', None)
        baudrate = self.params.get('baudrate', 50)
        send_f0 = self.params.get('send_f0', 500)
        send_f1 = self.params.get('send_f1', 700)
        #send_f0 = self.params.get('recv_f0', 2250)   #debug
        #send_f1 = self.params.get('recv_f1', 3150)   #debug
        send_f = [send_f0, send_f1, (send_f0+send_f1)/2]
        zcarrier = self.params.get('zcarrier', False)

        Fpb = int(sample_f / baudrate + 0.5)   # Frames per bit
        Fpw = int(Fpb * 7.5 + 0.5)   # Frames per wave

        time.sleep(0.5)

        waves = []
        for i in range(3):
            samples=[]
            for n in range(Fpb):
                t = n / sample_f
                s = math.sin(t * 2 * math.pi * send_f[i])
                samples.append(int(s*32000))   # 16 bit
            waves.append(struct.pack('%sh' % Fpb, *samples))   # 16 bit

        audio = pyaudio.PyAudio()
        stream = audio.open(format=pyaudio.paInt16, channels=1, rate=sample_f, output=True, input=False, output_device_index=devindex, input_device_index=devindex)

        #a = stream.get_write_available()
        try:

            while self.run:
                # Process buffer if we're online or if something's left in it
                # after going offline. Critical for ASCII services that send
                # faster than 50 Bd.
                if self._is_online.is_set() or self._tx_buffer:
                    if self._tx_buffer:
                        a = self._tx_buffer.pop(0)
                        if a == '§W':
                            bb = (0xFFC0,)
                            nbit = 16
                        elif a == '§A':
                            bb = (0xFFC0,)
                            nbit = 16
                        else:
                            bb = self._mc.encodeA2BM(a)
                            if not bb:
                                continue
                            nbit = 5
                        
                        for b in bb:
                            bits = [0]*nbit
                            mask = 1
                            for i in range(nbit):
                                if b & mask:
                                    bits[i] = 1
                                mask <<= 1
                            wavecomp = bytearray()
                            for bit in bits:
                                wavecomp.extend(waves[bit])
 
                            if nbit == 5:
                                # Single Baudot character: add start and stop bits
                                wavecomp[0:0] = waves[0]
                                wavecomp.extend(waves[1])
                                wavecomp.extend(waves[1])
                                # Limit send length (only 1.5 stop bits)
                                frames = Fpw   # 7.5 bits
                            else:
                                frames = len(wavecomp) // 2   # 16 bit words
                            stream.write(bytes(wavecomp), frames)   # blocking

                    else:   # nothing to send
                        stream.write(waves[1], Fpb)   # blocking

                else:   # offline
                    if self.delay_offline:
                        # delay going offline by 3 s
                        self.delay_offline = False
                        for i in range(150):
                            stream.write(waves[1], Fpb)   # blocking

                    if zcarrier:
                        stream.write(waves[0], Fpb)   # blocking
                    else:
                        # If there's absolutely nothing to do, block until
                        # we're going online again
                        self._is_online.wait()

                time.sleep(0.001)

        except Exception as e:
            print(e)

        finally:
            stream.stop_stream()  
            stream.close()

    # =====

    def thread_rx(self):
        """Handler for receiving tones."""

        # If the "0" bit counter was initialised to 0, after startup, we'd
        # receive a rogue ST press after 100 scans, ending any connection that
        # may be active. So initialise to 100.
        #
        # (If we wanted to press ST to achieve anything, our teleprinter would
        # have to be sending 1s before, which would reset the "0" counter. So,
        # no danger of interfering.)
        _bit_counter_0 = 100
        _bit_counter_1 = 0
        slice_counter = 0
        properly_online = False
        
        devindex = self.params.get('devindex', None)
        baudrate = self.params.get('baudrate', 50)

        # One slice is a quarter of a bit or 5 ms
        FpS = int(sample_f / baudrate / 4 + 0.5)   # Frames per slice

        time.sleep(1.5)

        audio = pyaudio.PyAudio()
        stream = audio.open(format=pyaudio.paInt16, channels=1, rate=sample_f, output=False, input=True, frames_per_buffer=FpS, input_device_index=devindex)

        while self.run:
            # Don't waste processor cycles while offline; wait up to 5 s before
            # next tone decode. If we come online during the wait, we
            # immediately scan for the next bit.
            #
            # This delay probably shouldn't be raised much. On one hand, the
            # worst-case reaction time will grow.  Moreover, the receive IIR
            # filter also seems to introduce a delay.  In trials under optimal
            # circumstances, after pressing AT on the teletypewriter, it took
            # the filter two cycles to recognise the change.
            self._is_online.wait(2)

            bdata = stream.read(FpS, exception_on_overflow=False)   # blocking
            data = np.frombuffer(bdata, dtype=np.int16)

            bit = self._recv_decode(data)

            #if bit is None and self._is_online.is_set():
            #print(bit, val)

            if bit:
                _bit_counter_0 = 0
                _bit_counter_1 += 1
                # In offline state, we wait quite a bit to save CPU cycles.
                # Before, we waited until the counter had increased to 20,
                # which means 100 ms.
                #
                # So react to the first "high" and go online.
                if _bit_counter_1 == 1 and not self._is_online.is_set():
                    self._rx_buffer.append('\x1bAT')
            else:
                _bit_counter_0 += 1
                _bit_counter_1 = 0
                if _bit_counter_0 == 100:   # 0.5sec
                    self._rx_buffer.append('\x1bST')
                    self._ST_pressed = True
            #l.debug("bit counters: 0:{} 1:{}".format(_bit_counter_0, _bit_counter_1))

            # Suppress symbol recognition until we're "properly online", i.e.
            # piTelex is in online state and at least one Z has been received
            # from the teletypewriter.
            #
            # If we don't wait for a stable Z, we might spuriously decode one
            # of these symbols (start bit, 5x character bit, stop bits):
            #
            # ScccccSs
            # ========
            # AAAAAAZZ: NULL (~ in piTelex)
            # AAAAAZZZ: T
            # AAAAZZZZ: O
            # AAAZZZZZ: M
            # AAZZZZZZ: V
            # AZZZZZZZ: letter shift ([ in piTelex)
            #
            # We must not detect any A level that only results from the earlier
            # offline state -- this would be the start bit triggering one of
            # the above characters. For the two possible ways of going online
            # this means:
            #
            # - AT is pressed: All ok, we've got a stable Z level already,
            #   that's why we went online in the first place.
            #
            # - Incoming connection: We send Z first, the teletypewriter
            #   acknowledges this by switching from A to Z after some time.
            #
            # The second case is critical: We have to wait for the
            # teletypewriter to send a Z; only after this we are "properly
            # online".
            if self._is_online.is_set():
                # If we're online and receive Z, set "properly online" status
                # to start reading characters
                if (not properly_online) and bit:
                    properly_online = True
                    slice_counter = 0
            else:
                properly_online = False
                continue

            # If we haven't received a Z yet, skip character recognition until
            # we do
            if not properly_online:
                continue

            if slice_counter == 0:
                if not bit:   # found start step
                    symbol = 0
                    slice_counter = 1

            else:
                if slice_counter in (1, 2):   # middle of start step
                    if bit: # check if correct start bit
                        slice_counter = -1
                if slice_counter == 6:   # middle of step 1
                    if bit:
                        symbol |= 1
                if slice_counter == 10:   # middle of step 2
                    if bit:
                        symbol |= 2
                if slice_counter == 14:   # middle of step 3
                    if bit:
                        symbol |= 4
                if slice_counter == 18:   # middle of step 4
                    if bit:
                        symbol |= 8
                if slice_counter == 22:   # middle of step 5
                    if bit:
                        symbol |= 16
                if slice_counter == 26:   # middle of stop step
                    if not bit:
                        slice_counter = -5
                        pass
                if slice_counter >= 28:   # end of stop step
                    slice_counter = 0
                    #print(symbol, val)   #debug
                    a = self._mc.decodeBM2A([symbol])
                    if a:
                        self._rx_buffer.append(a)
                    continue

                slice_counter += 1
        
            #time.sleep(0.001)


        stream.stop_stream()  
        stream.close()

    # =====

    # IIR-filter
    def _recv_decode_init(self, recv_f):
        self._filters = []
        for i in range(2):
            f = recv_f[i]
            filter_bp = signal.iirfilter(4, [f/1.05, f*1.05], rs=40, btype='bandpass',
                        analog=False, ftype='butter', fs=sample_f, output='sos')
            self._filters.append(filter_bp)

        if not plot_spectrum:
            return

        import matplotlib.pyplot as plt
        plt.figure()
        plt.ylim(-100, 5)
        plt.xlim(0, 5500)
        plt.grid(True)
        plt.xlabel('Frequency (Hz)')
        plt.ylabel('Gain (dB)')
        plt.title('{}Hz, {}Hz'.format(recv_f[0], recv_f[1]))

        for i in range(2):
            f = recv_f[i]
            w, h = signal.sosfreqz(self._filters[i], 2000, fs=sample_f)
            plt.plot(w, 20*np.log10(np.abs(h)), label=str(f)+'Hz')
            plt.plot((f,f), (10, -100), color='red', linestyle='dashed')

        plt.plot((500,500), (10, -100), color='blue', linestyle='dashed')
        plt.plot((700,700), (10, -100), color='blue', linestyle='dashed')
        plt.show()

    # -----

    # IIR-filter
    def _recv_decode(self, data):
        val = [None, None]
        for i in range(2):
            fdata = signal.sosfilt(self._filters[i], data)
            fdata = np.abs(fdata)   # rectifier - instead of envelope curve
            val[i] = int(np.average(fdata))   # get energy for each frequency band

        bit = val[0] < val[1]   # compare energy of each frequency band
        if (val[0] + val[1]) < self.recv_squelch:   # no carrier
            bit = None

        if self.recv_debug:
            with open('recv_debug.log', 'a') as fp:
                line = '{},{}\n'.format(val[0], val[1])
                fp.write(line)

        return bit

    # =====

    # FIR-filter - not longer used
    def _recv_decode_init_FIR(self, recv_f):
        self._filters = []
        fbw = [(recv_f[1] - recv_f[0]) * 0.85, (recv_f[1] - recv_f[0]) * 0.8]
        for i in range(2):
            f = recv_f[i]
            filter_bp = signal.remez(80, [0, f-fbw[i], f, f, f+fbw[i], sample_f/2], [0,1,0], fs=sample_f, maxiter=100)
            self._filters.append(filter_bp)

        if not plot_spectrum:
            return

        import matplotlib.pyplot as plt
        plt.figure()
        plt.ylim(-60, 5)
        plt.xlim(0, 5500)
        plt.grid(True)
        plt.xlabel('Frequency (Hz)')
        plt.ylabel('Gain (dB)')
        plt.title('{}Hz, {}Hz'.format(recv_f[0], recv_f[1]))

        fbw = [(recv_f[1] - recv_f[0]) * 0.85, (recv_f[1] - recv_f[0]) * 0.8]
        for i in range(2):
            f = recv_f[i]
            w, h = signal.freqz(self._filters[i], [1], worN=2500)
            plt.plot(0.5*sample_f*w/np.pi, 20*np.log10(np.abs(h)))
            plt.plot((f,f), (10, -100), color='red', linestyle='dashed')

        plt.plot((500,500), (10, -100), color='blue', linestyle='dashed')
        plt.plot((700,700), (10, -100), color='blue', linestyle='dashed')
        plt.show()

    # -----

    # FIR-filter - not longer used
    def _recv_decode_FIR(self, data):
        val = [None, None]
        for i in range(2):
            fdata = signal.lfilter(self._filters[i], 1, data)
            fdata = np.abs(fdata)
            val[i] = np.average(fdata)

        bit = val[0] < val[1]
        if (val[0] + val[1]) < self.recv_squelch:   # no carrier
            bit = None
        return bit

#######

