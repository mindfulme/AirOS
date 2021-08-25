import os
import subprocess
import threading
import time
from copy import deepcopy
from typing import Any, List, Optional, Set, Tuple

import psutil
from loguru import logger

from exceptions import ArdupilotProcessKillFail
from firmware.FirmwareDownload import Platform, Vehicle
from firmware.FirmwareManagement import FirmwareManager
from flight_controller_detector.Detector import Detector as BoardDetector
from flight_controller_detector.Detector import FlightControllerType
from mavlink_comm.VehicleManager import VehicleManager
from mavlink_proxy.Endpoint import Endpoint, EndpointType
from mavlink_proxy.Manager import Manager as MavlinkManager
from settings import Settings
from Singleton import Singleton
from typedefs import SITLFrame


class ArduPilotManager(metaclass=Singleton):
    # pylint: disable=too-many-instance-attributes
    def __init__(self) -> None:
        self.settings = Settings()
        self.mavlink_manager = MavlinkManager()
        self.mavlink_manager.set_logdir(self.settings.log_path)
        self._current_platform: Platform = Platform.Undefined
        self._current_sitl_frame: SITLFrame = SITLFrame.UNDEFINED

        # Load settings and do the initial configuration
        if self.settings.load():
            logger.info(f"Loaded settings from {self.settings.settings_file}.")
            logger.debug(self.settings.content)
        else:
            self.settings.create_settings_file()

        self.configuration = deepcopy(self.settings.content)
        self._load_endpoints()
        self.subprocess: Optional[Any] = None
        self.firmware_manager = FirmwareManager(self.settings.firmware_folder)
        self.vehicle_manager = VehicleManager()

        self.should_be_running = False
        auto_restart_thread = threading.Thread(target=self.auto_restart, args=())
        auto_restart_thread.daemon = True
        auto_restart_thread.start()

    def auto_restart(self) -> None:
        """Auto-restart Ardupilot process if it dies when not supposed to."""
        while True:
            subprocess_stopped = self.subprocess is not None and self.subprocess.poll() is not None
            needs_restart = (
                self.should_be_running
                and self.current_platform in [Platform.SITL, Platform.Navigator]
                and (len(self.running_ardupilot_processes()) == 0 or subprocess_stopped)
            )
            if needs_restart:
                logger.debug("Restarting ardupilot...")
                self.start_ardupilot()
            time.sleep(5.0)

    def run_with_board(self) -> None:
        ArduPilotManager.check_running_as_root()

        while not self.start_board(BoardDetector.detect()):
            logger.warning("Flight controller board not detected, will try again.")
            time.sleep(2)

    @staticmethod
    def check_running_as_root() -> None:
        if os.geteuid() != 0:
            raise RuntimeError("ArduPilot manager needs to run with root privilege.")

    @property
    def current_platform(self) -> Platform:
        return self._current_platform

    @current_platform.setter
    def current_platform(self, platform: Platform) -> None:
        self._current_platform = platform
        logger.info(f"Setting {platform} as current platform.")

    @property
    def current_sitl_frame(self) -> SITLFrame:
        return self._current_sitl_frame

    @current_sitl_frame.setter
    def current_sitl_frame(self, frame: SITLFrame) -> None:
        self._current_sitl_frame = frame
        logger.info(f"Setting {frame.value} as frame for SITL.")

    def start_navigator(self) -> None:
        self.current_platform = Platform.Navigator
        if not self.firmware_manager.is_firmware_installed(self.current_platform):
            self.firmware_manager.install_firmware_from_params(Vehicle.Sub, self.current_platform)

        # ArduPilot process will connect as a client on the UDP server created by the mavlink router
        master_endpoint = Endpoint(
            "Master", self.settings.app_name, EndpointType.UDPServer, "127.0.0.1", 8852, protected=True
        )

        # Run ardupilot inside while loop to avoid exiting after reboot command
        ## Can be changed back to a simple command after https://github.com/ArduPilot/ardupilot/issues/17572
        ## gets fixed.
        # pylint: disable=consider-using-with
        #
        # The mapping of serial ports works as in the following table:
        #
        # |    ArduSub   |       Navigator         |
        # | -C = Serial1 | Serial1 => /dev/ttyS0   |
        # | -B = Serial3 | Serial3 => /dev/ttyAMA1 |
        # | -E = Serial4 | Serial4 => /dev/ttyAMA2 |
        # | -F = Serial5 | Serial5 => /dev/ttyAMA3 |
        #
        # The first column comes from https://ardupilot.org/dev/docs/sitl-serial-mapping.html

        self.subprocess = subprocess.Popen(
            f"{self.firmware_manager.firmware_path(self.current_platform)}"
            f" -A udp:{master_endpoint.place}:{master_endpoint.argument}"
            f" --log-directory {self.settings.firmware_folder}/logs/"
            f" --storage-directory {self.settings.firmware_folder}/storage/"
            f" -C /dev/ttyS0"
            f" -B /dev/ttyAMA1"
            f" -E /dev/ttyAMA2"
            f" -F /dev/ttyAMA3",
            shell=True,
            encoding="utf-8",
            errors="ignore",
        )

        self.start_mavlink_manager(master_endpoint)

    def start_serial(self, device: str) -> None:
        self.current_platform = Platform.Pixhawk1
        self.start_mavlink_manager(
            Endpoint("Master", self.settings.app_name, EndpointType.Serial, device, 115200, protected=True)
        )

    def run_with_sitl(self, frame: SITLFrame = SITLFrame.VECTORED) -> None:
        self.current_platform = Platform.SITL
        if not self.firmware_manager.is_firmware_installed(self.current_platform):
            self.firmware_manager.install_firmware_from_params(Vehicle.Sub, self.current_platform)
        if frame == SITLFrame.UNDEFINED:
            frame = SITLFrame.VECTORED
            logger.warning(f"SITL frame is undefined. Setting {frame} as current frame.")
        self.current_sitl_frame = frame

        # ArduPilot SITL binary will bind TCP port 5760 (server) and the mavlink router will connect to it as a client
        master_endpoint = Endpoint(
            "Master", self.settings.app_name, EndpointType.TCPServer, "127.0.0.1", 5760, protected=True
        )
        # The mapping of serial ports works as in the following table:
        #
        # |    ArduSub   |       Navigator         |
        # | -C = Serial1 | Serial1 => /dev/ttyS0   |
        # | -B = Serial3 | Serial3 => /dev/ttyAMA1 |
        # | -E = Serial4 | Serial4 => /dev/ttyAMA2 |
        # | -F = Serial5 | Serial5 => /dev/ttyAMA3 |
        #
        # pylint: disable=consider-using-with
        self.subprocess = subprocess.Popen(
            [
                self.firmware_manager.firmware_path(self.current_platform),
                "--model",
                self.current_sitl_frame.value,
                "--base-port",
                str(master_endpoint.argument),
                "--home",
                "-27.563,-48.459,0.0,270.0",
            ],
            shell=False,
            encoding="utf-8",
            errors="ignore",
        )

        self.start_mavlink_manager(master_endpoint)

    def start_mavlink_manager(self, device: Endpoint) -> None:
        try:
            self.add_new_endpoints(
                {
                    Endpoint(
                        "GCS Link",
                        self.settings.app_name,
                        EndpointType.UDPServer,
                        "0.0.0.0",
                        14550,
                        protected=True,
                    )
                }
            )
            self.add_new_endpoints(
                {
                    Endpoint(
                        "MAVLink2Rest",
                        self.settings.app_name,
                        EndpointType.UDPClient,
                        "127.0.0.1",
                        14000,
                        protected=True,
                    )
                }
            )
        except Exception as error:
            logger.error(f"Could not create default GCS endpoint: {error}")
        self.mavlink_manager.set_master_endpoint(device)
        self.mavlink_manager.start()

    def start_board(self, boards: List[Tuple[FlightControllerType, str]]) -> bool:
        if not boards:
            return False

        if len(boards) > 1:
            logger.warning(f"More than a single board detected: {boards}")

        # Sort by priority
        boards.sort(key=lambda tup: tup[0].value)

        flight_controller_type, place = boards[0]
        logger.info(f"Board in use: {flight_controller_type.name}.")

        if FlightControllerType.Navigator == flight_controller_type:
            self.start_navigator()
            return True
        if FlightControllerType.Serial == flight_controller_type:
            self.start_serial(place)
            return True
        raise RuntimeError("Invalid board type: {boards}")

    def running_ardupilot_processes(self) -> List[psutil.Process]:
        """Return list of all Ardupilot process running on system."""

        def is_ardupilot_process(process: psutil.Process) -> bool:
            """Checks if given process is using Ardupilot's firmware file for current platform."""
            return str(self.firmware_manager.firmware_path(self.current_platform)) in " ".join(process.cmdline())

        return list(filter(is_ardupilot_process, psutil.process_iter()))

    def terminate_ardupilot_subprocess(self) -> None:
        """Terminate Ardupilot subprocess."""
        if self.subprocess:
            self.subprocess.terminate()
            for _ in range(10):
                if self.subprocess.poll() is not None:
                    logger.info("Ardupilot subprocess terminated.")
                    return
                logger.debug("Waiting for process to die...")
                time.sleep(0.5)
            raise ArdupilotProcessKillFail("Could not terminate Ardupilot subprocess.")
        logger.warning("Ardupilot subprocess already not running.")

    def prune_ardupilot_processes(self) -> None:
        """Kill all system processes using Ardupilot's firmware file."""
        for process in self.running_ardupilot_processes():
            try:
                logger.debug(f"Killing Ardupilot process {process.name()}::{process.pid}.")
                process.kill()
                time.sleep(0.5)
            except Exception as error:
                raise ArdupilotProcessKillFail(f"Could not kill {process.name()}::{process.pid}.") from error

    def kill_ardupilot(self) -> None:
        self.should_be_running = False

        if not self.current_platform == Platform.SITL:
            logger.info("Disarming vehicle.")
            self.vehicle_manager.disarm_vehicle()
            logger.info("Vehicle disarmed.")

        # TODO: Add shutdown command on HAL_SITL and HAL_LINUX, changing terminate/prune
        # logic with a simple "self.vehicle_manager.shutdown_vehicle()"
        logger.info("Terminating Ardupilot subprocess.")
        self.terminate_ardupilot_subprocess()
        logger.info("Ardupilot subprocess terminated.")
        logger.info("Pruning Ardupilot's system processes.")
        self.prune_ardupilot_processes()
        logger.info("Ardupilot's system processes pruned.")

        logger.info("Stopping Mavlink manager.")
        self.mavlink_manager.stop()
        logger.info("Mavlink manager stopped.")

    def start_ardupilot(self) -> None:
        if self.current_platform == Platform.SITL:
            self.run_with_sitl(self.current_sitl_frame)
            self.should_be_running = True
            return
        self.run_with_board()
        self.should_be_running = True

    def restart_ardupilot(self) -> None:
        self.kill_ardupilot()
        self.start_ardupilot()

    def _get_configuration_endpoints(self) -> Set[Endpoint]:
        return {Endpoint(**endpoint) for endpoint in self.configuration.get("endpoints") or []}

    def _save_endpoints_to_configuration(self, endpoints: Set[Endpoint]) -> None:
        self.configuration["endpoints"] = list(map(Endpoint.as_dict, endpoints))

    def _load_endpoints(self) -> None:
        """Load endpoints from the configuration file to the mavlink manager."""
        for endpoint in self._get_configuration_endpoints():
            try:
                self.mavlink_manager.add_endpoint(endpoint)
            except Exception as error:
                logger.error(f"Could not load endpoint {endpoint}: {error}")

    def _reset_endpoints(self, endpoints: Set[Endpoint]) -> None:
        try:
            self.mavlink_manager.clear_endpoints()
            self.mavlink_manager.add_endpoints(endpoints)
            logger.info("Resetting endpoints to previous state.")
        except Exception as error:
            logger.error(f"Error resetting endpoints: {error}")

    def reload_endpoints(self) -> None:
        try:
            persistent_endpoints = set(filter(lambda endpoint: endpoint.persistent, self.get_endpoints()))
            self._save_endpoints_to_configuration(persistent_endpoints)
            self.settings.save(self.configuration)
            self.mavlink_manager.restart()
        except Exception as error:
            logger.error(f"Error updating endpoints: {error}")

    def get_endpoints(self) -> Set[Endpoint]:
        """Get all endpoints from the mavlink manager."""
        return self.mavlink_manager.endpoints()

    def add_new_endpoints(self, new_endpoints: Set[Endpoint]) -> None:
        """Add multiple endpoints to the mavlink manager and save them on the configuration file."""
        loaded_endpoints = self.get_endpoints()

        for endpoint in new_endpoints:
            try:
                self.mavlink_manager.add_endpoint(endpoint)
                logger.info(f"Adding endpoint '{endpoint.name}' and saving it to the settings file.")
            except Exception as error:
                logger.error(f"Failed to add endpoint '{endpoint.name}': {error}")
                self._reset_endpoints(loaded_endpoints)
                raise

        self.reload_endpoints()

    def remove_endpoints(self, endpoints_to_remove: Set[Endpoint]) -> None:
        """Remove multiple endpoints from the mavlink manager and save them on the configuration file."""
        loaded_endpoints = self.get_endpoints()

        protected_endpoints = set(filter(lambda endpoint: endpoint.protected, endpoints_to_remove))
        if protected_endpoints:
            raise ValueError(f"Endpoints {protected_endpoints} are protected. Aborting operation.")

        for endpoint in endpoints_to_remove:
            try:
                self.mavlink_manager.remove_endpoint(endpoint)
                logger.info(f"Deleting endpoint '{endpoint.name}' and removing it from the settings file.")
            except Exception as error:
                logger.error(f"Failed to remove endpoint '{endpoint.name}': {error}")
                self._reset_endpoints(loaded_endpoints)
                raise

        self.reload_endpoints()
