from typing import Any, Union

import os
import sys
import time
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from smbus2 import SMBus

from opensourceleg.joints import Joint
from opensourceleg.logger import Logger


class StrainAmp:
    """
    A class to directly manage the 6ch strain gauge amplifier over I2C.
    Author: Mitry Anderson
    """

    # register numbers for the "ezi2c" interface on the strainamp
    # found in source code here: https://github.com/JFDuval/flexsea-strain/tree/dev
    MEM_R_CH1_H = 8
    MEM_R_CH1_L = 9
    MEM_R_CH2_H = 10
    MEM_R_CH2_L = 11
    MEM_R_CH3_H = 12
    MEM_R_CH3_L = 13
    MEM_R_CH4_H = 14
    MEM_R_CH4_L = 15
    MEM_R_CH5_H = 16
    MEM_R_CH5_L = 17
    MEM_R_CH6_H = 18
    MEM_R_CH6_L = 19

    def __init__(self, bus, I2C_addr=0x66) -> None:
        """Create a strainamp object, to talk over I2C"""
        # self._I2CMan = I2CManager(bus)
        self._SMBus: Union[SMBus, MockSMBus] = SMBus(bus)
        time.sleep(1)
        self.bus = bus
        self.addr = I2C_addr
        self.genvars = np.zeros((3, 6))
        self.indx = 0
        self.is_streaming = True
        self.data: list[int] = []
        self.failed_reads = 0

    def read_uncompressed_strain(self):
        """Used for an older version of the strain amp firmware (at least pre-2017)"""
        data = []
        for i in range(self.MEM_R_CH1_H, self.MEM_R_CH6_L + 1):
            data.append(self._SMBus.read_byte_data(self.addr, i))

        return self.unpack_uncompressed_strain(data)

    def read_compressed_strain(self):
        """Used for more recent versions of strain amp firmware"""
        try:
            self.data = self._SMBus.read_i2c_block_data(self.addr, self.MEM_R_CH1_H, 10)
            self.failed_reads = 0
        except OSError as e:
            self.failed_reads += 1
            # print("\n read failed")
            if self.failed_reads >= 5:
                raise Exception("Load cell unresponsive.")
        # unpack them and return as nparray
        return self.unpack_compressed_strain(self.data)

    def update(self):
        """Called to update data of strain amp. Also returns data."""
        self.genvars[self.indx, :] = self.read_compressed_strain()
        self.indx: int = (self.indx + 1) % 3
        return np.median(a=self.genvars, axis=0)

    @staticmethod
    def unpack_uncompressed_strain(data):
        """Used for an older version of the strain amp firmware (at least pre-2017)"""
        ch1 = (data[0] << 8) | data[1]
        ch2 = (data[2] << 8) | data[3]
        ch3 = (data[4] << 8) | data[5]
        ch4 = (data[6] << 8) | data[7]
        ch5 = (data[8] << 8) | data[9]
        ch6 = (data[10] << 8) | data[11]
        return np.array(object=[ch1, ch2, ch3, ch4, ch5, ch6])

    @staticmethod
    def unpack_compressed_strain(data):
        """Used for more recent versions of strainamp firmware"""
        # ch1 = (data[0] << 4) | ( (data[1] >> 4) & 0x0F)
        # ch2 = ( (data[1] << 8) & 0x0F00) | data[2]
        # ch3 = (data[3] << 4) | ( (data[4] >> 4) & 0x0F)
        # ch4 = ( (data[4] << 8) & 0x0F00) | data[5]
        # ch5 = (data[6] << 4) | ( (data[7] >> 4) & 0x0F)
        # ch6 = ( (data[7] << 8) & 0x0F00) | data[8]
        # moved into one line to save 0.02ms -- maybe pointless but eh
        return np.array(
            object=[
                (data[0] << 4) | ((data[1] >> 4) & 0x0F),
                ((data[1] << 8) & 0x0F00) | data[2],
                (data[3] << 4) | ((data[4] >> 4) & 0x0F),
                ((data[4] << 8) & 0x0F00) | data[5],
                (data[6] << 4) | ((data[7] >> 4) & 0x0F),
                ((data[7] << 8) & 0x0F00) | data[8],
            ]
        )

    @staticmethod
    def strain_data_to_wrench(
        unpacked_strain, loadcell_matrix, loadcell_zero, exc=5, gain=125
    ):
        """Converts strain values between 0 and 4095 to a wrench in N and Nm"""
        loadcell_signed = (unpacked_strain - 2048) / 4095 * exc
        loadcell_coupled = loadcell_signed * 1000 / (exc * gain)
        return np.reshape(
            np.transpose(a=loadcell_matrix.dot(np.transpose(a=loadcell_coupled)))
            - loadcell_zero,
            (6,),
        )

    @staticmethod
    def wrench_to_strain_data(measurement, loadcell_matrix, exc=5, gain=125):
        """Wrench in N and Nm to the strain values that would give that wrench"""
        loadcell_coupled = (np.linalg.inv(loadcell_matrix)).dot(measurement)
        loadcell_signed = loadcell_coupled * (exc * gain) / 1000
        return ((loadcell_signed / exc) * 4095 + 2048).round(0).astype(int)


class Loadcell:
    def __init__(
        self,
        dephy_mode: bool = False,
        joint: Joint = None,
        amp_gain: float = 125.0,
        exc: float = 5.0,
        loadcell_matrix=None,
        logger: Logger = None,
    ) -> None:
        self._is_dephy: bool = dephy_mode
        self._joint: Joint = joint  # type: ignore
        self._amp_gain: float = amp_gain
        self._exc: float = exc
        self._adc_range: int = 2**12 - 1
        self._offset: float = (2**12) / 2
        self._lc = None

        if not self._is_dephy:
            self._lc = StrainAmp(bus=1, I2C_addr=0x66)

        self._loadcell_matrix = loadcell_matrix
        self._loadcell_data: npt.NDArray[np.double] = np.zeros(
            shape=(1, 6), dtype=np.double
        )
        self._prev_loadcell_data: npt.NDArray[np.double] = np.zeros(
            shape=(1, 6), dtype=np.double
        )

        self._loadcell_zero: npt.NDArray[np.double] = np.zeros(
            shape=(1, 6), dtype=np.double
        )
        self._zeroed = False
        self._log: Logger = logger  # type: ignore

    def reset(self):
        self._zeroed = False
        self._loadcell_zero = np.zeros(shape=(1, 6), dtype=np.double)

    def update(self, loadcell_zero=None) -> None:
        """
        Computes Loadcell data

        """
        if self._is_dephy:
            loadcell_signed = (
                (self._joint.genvars - self._offset) / self._adc_range * self._exc
            )
        else:
            assert self._lc is not None
            loadcell_signed = (
                (self._lc.update() - self._offset) / self._adc_range * self._exc
            )

        loadcell_coupled = loadcell_signed * 1000 / (self._exc * self._amp_gain)

        if loadcell_zero is None:

            self._loadcell_data = (
                np.transpose(
                    a=self._loadcell_matrix.dot(b=np.transpose(a=loadcell_coupled))
                )
                - self._loadcell_zero
            )

        else:
            self._loadcell_data = (
                np.transpose(
                    a=self._loadcell_matrix.dot(b=np.transpose(a=loadcell_coupled))
                )
                - loadcell_zero
            )

    def initialize(self, number_of_iterations: int = 2000) -> None:
        """
        Obtains the initial loadcell reading (aka) loadcell_zero
        """
        ideal_loadcell_zero = np.zeros(shape=(1, 6), dtype=np.double)

        if not self._zeroed:
            self._log.info(
                f"[LOADCELL] Initiating zeroing routine, please ensure that there is no ground contact force."
            )
            time.sleep(1)

            if self._is_dephy:
                if self._joint.is_streaming:
                    self._joint.update()
                    self.update()
                else:
                    self._log.warning(
                        msg=f"[Loadcell] {self._joint.name} joint isn't streaming data. Please start streaming data before initializing loadcell."
                    )
                    return
            else:
                self.update()

            self._loadcell_zero = self._loadcell_data

            for _ in range(number_of_iterations):
                self.update(ideal_loadcell_zero)
                loadcell_offset = self._loadcell_data
                self._loadcell_zero = (loadcell_offset + self._loadcell_zero) / 2.0

            self._zeroed = True
            self._log.info(f"[LOADCELL] Zeroing routine complete.")

        elif (
            input(f"[Loadcell] Would you like to re-initialize loadcell? (y/n): ")
            == "y"
        ):
            self.reset()
            self.initialize()

    @property
    def is_zeroed(self) -> bool:
        return self._zeroed

    @property
    def fx(self):
        return self.loadcell_data[0]

    @property
    def fy(self):
        return self.loadcell_data[1]

    @property
    def fz(self):
        return self.loadcell_data[2]

    @property
    def mx(self):
        return self.loadcell_data[3]

    @property
    def my(self):
        return self.loadcell_data[4]

    @property
    def mz(self):
        return self.loadcell_data[5]

    @property
    def loadcell_data(self):
        if self._loadcell_data is not None:
            return self._loadcell_data[0]
        else:
            return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class MockSMBus:

    """
    Mocked SMBus class to test the StrainAmp class\n
    This class has attributes and methods that mimic the SMBus class
    but are implemented in a way to allow for testing.
    """

    MEM_R_CH1_H = 8
    MEM_R_CH1_L = 9
    MEM_R_CH2_H = 10
    MEM_R_CH2_L = 11
    MEM_R_CH3_H = 12
    MEM_R_CH3_L = 13
    MEM_R_CH4_H = 14
    MEM_R_CH4_L = 15
    MEM_R_CH5_H = 16
    MEM_R_CH5_L = 17
    MEM_R_CH6_H = 18
    MEM_R_CH6_L = 19

    # Initialize attributes needed for testing
    def __init__(self, bus: int = 1) -> None:
        self._bus = bus
        self._byte_data = bytearray(
            [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        )

    # Override the read_byte_data method to return the byte data
    def read_byte_data(
        self, I2C_addr: int = 1, register: int = 0, force: bool = False
    ) -> int:
        return self._byte_data[register]

    # Override the read_i2c_block_data method to return the byte data
    def read_i2c_block_data(
        self,
        I2C_addr: int = 1,
        register: int = 0,
        length: int = 10,
        force: bool = False,
    ) -> list[int]:
        data = []
        for i in range(length):
            data.append(int(self._byte_data[i]))
        return data


class MockStrainAmp(StrainAmp):

    """
    Create a mock StrainAmp class to test the StrainAmp and Loadcell classes\n
    This class inherits from the StrainAmp class but overrides the _SMBus atttribute
    with a MockSMBus object.
    """

    def __init__(self, bus: int = 1, I2C_addr=0x66) -> None:
        self._SMBus = MockSMBus(bus=bus)
        self.bus = bus
        self.addr = I2C_addr
        self.genvars = np.zeros((3, 6))
        self.indx = 0
        self.is_streaming = True
        self.data = []
        self.failed_reads = 0


class MockLoadcell(Loadcell):

    """
    Create a mock Loadcell class to test the StrainAmp and Loadcell classes\n
    This class inherits from the Loadcell class but overrides the _lc atttribute
    with a MockStrainAmp object.
    """

    # Initialize the same way but with a mock StrainAmp
    def __init__(
        self,
        dephy_mode: bool = False,
        joint: Joint = None,
        amp_gain: float = 125.0,
        exc: float = 5.0,
        loadcell_matrix: npt.NDArray[np.double] = None,
        logger: Logger = None,
    ) -> None:
        self._is_dephy: bool = dephy_mode
        self._joint: Joint = joint  # type: ignore
        self._amp_gain: float = amp_gain
        self._exc: float = exc
        self._adc_range: int = 2**12 - 1
        self._offset: float = (2**12) / 2
        self._lc = None

        if not self._is_dephy:
            self._lc = MockStrainAmp()

        self._loadcell_matrix = loadcell_matrix
        self._loadcell_data: npt.NDArray[np.double] = np.zeros(
            shape=(1, 6), dtype=np.double
        )
        self._prev_loadcell_data: npt.NDArray[np.double] = np.zeros(
            shape=(1, 6), dtype=np.double
        )

        self._loadcell_zero = np.zeros(shape=(1, 6), dtype=np.double)
        self._zeroed = False
        self._log: Logger = logger  # type: ignore


@dataclass
class IMUDataClass:
    """
    Dataclass for IMU data

    Author: Kevin Best
    https://github.com/tkevinbest
    """

    angle_x: float = 0
    angle_y: float = 0
    angle_z: float = 0
    velocity_x: float = 0
    velocity_y: float = 0
    velocity_z: float = 0
    accel_x: float = 0
    accel_y: float = 0
    accel_z: float = 0
    imu_time_sta: float = 0
    imu_filter_gps_time_week_num: float = 0


class IMULordMicrostrain:
    def __init__(
        self, port=r"/dev/ttyUSB0", baud_rate=921600, timeout=500, sample_rate=100
    ):
        import mscl as ms

        self.port = port
        self.baud_rate = baud_rate
        self.connection = ms.Connection.Serial(
            os.path.realpath(self.port), self.baud_rate
        )
        self.imu = ms.InertialNode(self.connection)
        self.timeout = timeout  # Timeout in (ms) to read the IMU
        time.sleep(0.5)

        # Configure data channels
        channels = ms.MipChannels()
        channels.append(
            ms.MipChannel(
                ms.MipTypes.CH_FIELD_ESTFILTER_ESTIMATED_ORIENT_EULER,
                ms.SampleRate.Hertz(sampleRate),
            )
        )
        channels.append(
            ms.MipChannel(
                ms.MipTypes.CH_FIELD_ESTFILTER_ESTIMATED_ANGULAR_RATE,
                ms.SampleRate.Hertz(sampleRate),
            )
        )
        channels.append(
            ms.MipChannel(
                ms.MipTypes.CH_FIELD_ESTFILTER_ESTIMATED_LINEAR_ACCEL,
                ms.SampleRate.Hertz(sampleRate),
            )
        )
        channels.append(
            ms.MipChannel(
                ms.MipTypes.CH_FIELD_ESTFILTER_GPS_TIMESTAMP,
                ms.SampleRate.Hertz(sampleRate),
            )
        )

        self.imu.setActiveChannelFields(ms.MipTypes.CLASS_ESTFILTER, channels)
        self.imu.enableDataStream(ms.MipTypes.CLASS_ESTFILTER)
        self.imu.setToIdle()

        packets = self.imu.getDataPackets(
            self.timeout
        )  # Clean the internal circular buffer.
        self.imu_data = IMUDataClass()

    def start_streaming(self):
        self.imu.resume()

    def stop_streaming(self):
        self.imu.setToIdle()

    def get_data(self):
        """
        Get IMU data from the Lord Microstrain IMU
        """
        imu_packets = self.imu.getDataPackets(self.timeout)
        if len(imu_packets):
            # Read all the information from the first packet as float.
            raw_imu_data = {
                data_point.channelName(): data_point.as_float()
                for data_point in imu_packets[-1].data()
            }
            self.imu_data.angle_x = raw_imu_data["estRoll"]
            self.imu_data.angle_y = raw_imu_data["estPitch"]
            self.imu_data.angle_z = raw_imu_data["estYaw"]
            self.imu_data.velocity_x = raw_imu_data["estAngularRateX"]
            self.imu_data.velocity_y = raw_imu_data["estAngularRateY"]
            self.imu_data.velocity_z = raw_imu_data["estAngularRateZ"]
            self.imu_data.accel_x = raw_imu_data["estLinearAccelX"]
            self.imu_data.accel_y = raw_imu_data["estLinearAccelY"]
            self.imu_data.accel_z = raw_imu_data["estLinearAccelZ"]
            self.imu_data.imu_time_sta = raw_imu_data["estFilterGpsTimeTow"]
            self.imu_data.imu_filter_gps_time_week_num = raw_imu_data[
                "estFilterGpsTimeWeekNum"
            ]

        return self.imu_data


if __name__ == "__main__":
    imu = IMULordMicrostrain(r"/dev/ttyS0", timeout=0, sample_rate=100)
    imu.start_streaming()

    for i in range(500):
        imu_data = imu.get_data()
        time.sleep(0.01)
