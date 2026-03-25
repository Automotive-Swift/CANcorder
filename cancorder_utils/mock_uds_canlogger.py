#!/usr/bin/env python3
"""
Mock UDS-on-CAN Logger Simulator

Generates synthetic UDS (Unified Diagnostic Services) over CAN traffic that
simulates a typical ECU reprogramming session. Streams the frames via TCP
in the ECUconnect Logger binary format.

The simulated session includes:
- Diagnostic session control (extended/programming sessions)
- ECU identification reads
- Security access (seed & key)
- Memory erase via routine control
- Data transfer simulation
- ECU reset and DTC clearing

Packet format: [timestamp:8][id:4][flags:1][dlc:1][data:0-64]
"""

import argparse
import random
import socket
import sys
import threading
import time
from pathlib import Path
from typing import List, Tuple

from .zeroconf_service import LoggerZeroconfService, SERVICE_NAME_PREFIX
from .canlogger_common import (
    ts,
    color,
    find_available_port,
    zeroconf_log,
    pack_frame,
    ClientManager,
    tcp_server_thread,
    BINARY_PROTOCOL_HELP,
    TESTING_CONNECTION_HELP,
)


HELP_DESCRIPTION = """\
Mock UDS-on-CAN Logger Simulator
================================

Generates synthetic UDS (Unified Diagnostic Services) over CAN traffic that
simulates a typical ECU reprogramming/flashing session. The simulated session
follows a realistic diagnostic sequence including identification, security
access, memory operations, data transfer, and ECU reset.

By default runs in sync mode where UDS sessions only start when a client
connects. Use --async for free-running mode where sessions generate continuously.

Use this to test diagnostic applications without needing real CAN hardware
or an actual ECU.
"""

HELP_EPILOG = BINARY_PROTOCOL_HELP + """

SIMULATED UDS SERVICES
======================

The following UDS services are simulated in sequence:

  0x10  Diagnostic Session Control
        - Extended diagnostic session (0x03)
        - Programming session (0x02)

  0x22  Read Data By Identifier
        - VIN, ECU Serial Number, Software Version, etc.

  0x27  Security Access
        - Seed request and key response

  0x28  Communication Control
        - Disable normal message transmission

  0x85  Control DTC Setting
        - Disable DTC storage during programming

  0x31  Routine Control
        - Erase memory block
        - Check programming dependencies
        - Check programming preconditions

  0x34  Request Download
        - Prepare ECU for data transfer

  0x36  Transfer Data
        - Simulated block transfers with progress

  0x37  Request Transfer Exit
        - Complete transfer session

  0x19  Read DTC Information
        - Report number of DTCs by status mask (0x01)
        - Report DTCs by status mask (0x02)
        - Report DTC snapshot record (0x04)

  0x14  Clear Diagnostic Information
        - Clear all stored DTCs
        - Verify DTCs cleared after operation

  0x11  ECU Reset
        - Hard reset after programming


SIMULATED DTCs
==============

The simulator maintains a randomized set of DTCs that are:
  - Read before reprogramming (UDS Service 0x19)
  - Cleared after reprogramming (UDS Service 0x14)
  - Polled via OBD-II during inter-session traffic (Services 0x03, 0x07)

Sample DTCs include:
  P0300  Random/Multiple Cylinder Misfire
  P0171  System Too Lean Bank 1
  P0420  Catalyst System Efficiency Below Threshold
  P0442  EVAP System Leak Detected (small)
  C0035  Left Front Wheel Speed Sensor
  B0101  Interior Light Circuit Malfunction
  U0073  Control Module Communication Bus Off


OBD-II TRAFFIC
==============

Between UDS reprogramming sessions, the simulator generates OBD-II
polling traffic to simulate a scan tool monitoring vehicle data:

  Service 01 PIDs polled:
    0x05  Engine Coolant Temperature
    0x0C  Engine RPM
    0x0D  Vehicle Speed
    0x0F  Intake Air Temperature
    0x10  MAF Air Flow Rate
    0x11  Throttle Position
    0x1F  Run Time Since Engine Start
    0x2F  Fuel Tank Level
    0x46  Ambient Air Temperature

  Uses functional addressing (0x7DF) with responses on 0x7E8.
  Simulates realistic sensor value fluctuations during "driving".


ERROR SIMULATION
================

The simulator occasionally generates negative responses with retries:

  0x21  Busy - Repeat Request
        - ECU temporarily busy, tester retries after short delay
        - ~3%% probability on session control requests

  0x22  Conditions Not Correct
        - Programming preconditions not met (e.g., engine running)
        - ~2%% probability, followed by retry after delay

  0x35  Invalid Key
        - Security access key calculation error
        - ~15%% probability on first attempt, then successful retry

  0x71  Transfer Data Suspended
        - Temporary flash write issue
        - ~2%% probability during data transfer, block retried

  0x72  General Programming Failure
        - Flash memory write error
        - Block retried after error

  0x73  Wrong Block Sequence Counter
        - Block sequence mismatch
        - Correct block sent on retry


TIMING SIMULATION
=================

The simulator uses realistic timings:
  - Normal response: 10-50ms
  - Security access: 100-300ms (seed generation)
  - Security retry delay: 500-1000ms after invalid key
  - Memory erase: 500ms-2s with 0x78 pending responses
  - Data transfer: ~20ms per block
  - OBD-II polling: ~100ms per cycle (5-10 Hz)
  - ECU reset: 1-2s recovery time
  - Inter-session OBD-II: 15-30 polling cycles


EXAMPLES
========

Basic usage (sync mode - waits for client):
  python3 %(prog)s

Async mode (free-running):
  python3 %(prog)s --async

With verbose output:
  python3 %(prog)s -v

Custom ECU IDs (e.g., for powertrain ECU):
  python3 %(prog)s --tester-id 0x7E0 --ecu-id 0x7E8

Faster simulation (half timing):
  python3 %(prog)s --speed 2.0

Single session per client:
  python3 %(prog)s --no-loop


""" + TESTING_CONNECTION_HELP

# OBD-II Constants
OBD_FUNCTIONAL_REQUEST_ID = 0x7DF
OBD_PHYSICAL_REQUEST_ID = 0x7E0
OBD_RESPONSE_ID_BASE = 0x7E8

# OBD-II Service 01 PIDs (Current Data)
OBD_PID_SUPPORTED_01_20 = 0x00
OBD_PID_ENGINE_COOLANT_TEMP = 0x05
OBD_PID_ENGINE_RPM = 0x0C
OBD_PID_VEHICLE_SPEED = 0x0D
OBD_PID_INTAKE_AIR_TEMP = 0x0F
OBD_PID_MAF_FLOW = 0x10
OBD_PID_THROTTLE_POSITION = 0x11
OBD_PID_RUNTIME = 0x1F
OBD_PID_FUEL_LEVEL = 0x2F
OBD_PID_AMBIENT_TEMP = 0x46

# OBD-II DTC Services
OBD_SID_SHOW_STORED_DTCS = 0x03
OBD_SID_CLEAR_DTCS = 0x04
OBD_SID_SHOW_PENDING_DTCS = 0x07

# UDS Service IDs
SID_DIAGNOSTIC_SESSION_CONTROL = 0x10
SID_ECU_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC_INFO = 0x19
SID_READ_DATA_BY_ID = 0x22
SID_SECURITY_ACCESS = 0x27
SID_COMMUNICATION_CONTROL = 0x28
SID_ROUTINE_CONTROL = 0x31
SID_REQUEST_DOWNLOAD = 0x34
SID_TRANSFER_DATA = 0x36
SID_REQUEST_TRANSFER_EXIT = 0x37

# UDS Read DTC Information Sub-functions
DTC_REPORT_NUMBER_BY_STATUS_MASK = 0x01
DTC_REPORT_BY_STATUS_MASK = 0x02
DTC_REPORT_SNAPSHOT_ID = 0x03
DTC_REPORT_SNAPSHOT_BY_DTC = 0x04
DTC_REPORT_EXTENDED_DATA = 0x06
DTC_REPORT_SUPPORTED_DTC = 0x0A

# DTC Status Mask bits
DTC_STATUS_TEST_FAILED = 0x01
DTC_STATUS_TEST_FAILED_THIS_CYCLE = 0x02
DTC_STATUS_PENDING = 0x04
DTC_STATUS_CONFIRMED = 0x08
DTC_STATUS_TEST_NOT_COMPLETED_SINCE_CLEAR = 0x10
DTC_STATUS_TEST_FAILED_SINCE_CLEAR = 0x20
DTC_STATUS_TEST_NOT_COMPLETED_THIS_CYCLE = 0x40
DTC_STATUS_WARNING_INDICATOR = 0x80

# Simulated DTCs (DTC code, status byte, description)
# DTC format: 3 bytes - e.g., P0300 = 0x03 0x00, C0035 = 0xC0 0x35
SIMULATED_DTCS = [
    # Powertrain DTCs (P-codes)
    (0x0300, DTC_STATUS_CONFIRMED | DTC_STATUS_TEST_FAILED_SINCE_CLEAR, "Random/Multiple Cylinder Misfire"),
    (0x0171, DTC_STATUS_CONFIRMED | DTC_STATUS_PENDING, "System Too Lean Bank 1"),
    (0x0420, DTC_STATUS_CONFIRMED, "Catalyst System Efficiency Below Threshold"),
    (0x0442, DTC_STATUS_PENDING, "EVAP System Leak Detected (small)"),
    # Chassis DTCs (C-codes) - high byte has 0x40 offset
    (0x4035, DTC_STATUS_CONFIRMED | DTC_STATUS_WARNING_INDICATOR, "Left Front Wheel Speed Sensor"),
    # Body DTCs (B-codes) - high byte has 0x80 offset
    (0x8101, DTC_STATUS_CONFIRMED, "Interior Light Circuit Malfunction"),
    # Network DTCs (U-codes) - high byte has 0xC0 offset
    (0xC073, DTC_STATUS_PENDING, "Control Module Communication Bus Off"),
]
SID_CONTROL_DTC_SETTING = 0x85

# Negative Response Codes (NRCs)
NRC_GENERAL_REJECT = 0x10
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
NRC_INCORRECT_MESSAGE_LENGTH = 0x13
NRC_BUSY_REPEAT_REQUEST = 0x21
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_REQUEST_SEQUENCE_ERROR = 0x24
NRC_REQUEST_OUT_OF_RANGE = 0x31
NRC_SECURITY_ACCESS_DENIED = 0x33
NRC_INVALID_KEY = 0x35
NRC_EXCEEDED_NUMBER_OF_ATTEMPTS = 0x36
NRC_REQUIRED_TIME_DELAY = 0x37
NRC_UPLOAD_DOWNLOAD_NOT_ACCEPTED = 0x70
NRC_TRANSFER_DATA_SUSPENDED = 0x71
NRC_GENERAL_PROGRAMMING_FAILURE = 0x72
NRC_WRONG_BLOCK_SEQUENCE = 0x73
NRC_RESPONSE_PENDING = 0x78
NRC_SERVICE_NOT_SUPPORTED_IN_SESSION = 0x7F


def make_single_frame(data: bytes) -> bytes:
    """Create a CAN-TP Single Frame (SF) from payload data."""
    if len(data) > 7:
        raise ValueError("Single frame data too long")
    frame = bytes([len(data)]) + data
    return frame.ljust(8, b'\xCC')


def make_first_frame(total_len: int, data: bytes) -> bytes:
    """Create a CAN-TP First Frame (FF) header."""
    pci_hi = 0x10 | ((total_len >> 8) & 0x0F)
    pci_lo = total_len & 0xFF
    return bytes([pci_hi, pci_lo]) + data[:6]


def make_consecutive_frame(seq_num: int, data: bytes) -> bytes:
    """Create a CAN-TP Consecutive Frame (CF)."""
    pci = 0x20 | (seq_num & 0x0F)
    frame = bytes([pci]) + data[:7]
    return frame.ljust(8, b'\xCC')


def make_flow_control(block_size: int = 0, st_min: int = 10) -> bytes:
    """Create a CAN-TP Flow Control (FC) frame."""
    return bytes([0x30, block_size, st_min, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC])


def positive_response(sid: int) -> int:
    """Return the positive response SID for a given service."""
    return sid + 0x40


def negative_response(sid: int, nrc: int) -> bytes:
    """Create a negative response frame."""
    return make_single_frame(bytes([0x7F, sid, nrc]))


class UDSSession:
    """Generates a realistic UDS reprogramming session sequence."""

    # Probability of various error conditions (0.0 - 1.0)
    ERROR_RATE_BUSY = 0.03           # ECU busy, repeat request
    ERROR_RATE_CONDITIONS = 0.02     # Conditions not correct
    ERROR_RATE_SECURITY_FAIL = 0.15  # First security attempt fails
    ERROR_RATE_TRANSFER_ERROR = 0.02 # Transfer data errors
    ERROR_RATE_SEQUENCE_ERROR = 0.01 # Wrong block sequence

    def __init__(
        self,
        tester_id: int = 0x7E0,
        ecu_id: int = 0x7E8,
        speed: float = 1.0,
        verbose: bool = False
    ):
        self.tester_id = tester_id
        self.ecu_id = ecu_id
        self.speed = speed
        self.verbose = verbose
        self.block_counter = 0
        self._reset_dtcs()
        self._security_attempts = 0

    def _reset_dtcs(self):
        """Reset DTCs to initial simulated state."""
        self.stored_dtcs = list(SIMULATED_DTCS)
        random.shuffle(self.stored_dtcs)
        # Keep a random subset (3-7 DTCs)
        num_dtcs = random.randint(3, min(7, len(self.stored_dtcs)))
        self.stored_dtcs = self.stored_dtcs[:num_dtcs]

    def delay(self, base_ms: float, variance_ms: float = 0):
        """Sleep with timing scaled by speed factor."""
        if variance_ms > 0:
            ms = base_ms + random.uniform(-variance_ms, variance_ms)
        else:
            ms = base_ms
        time.sleep(max(0.001, ms / 1000.0 / self.speed))

    def log(self, msg: str):
        if self.verbose:
            print(f"{ts()} {color('[uds]', '35')} {msg}")

    def generate_session(self) -> List[Tuple[int, bytes, float]]:
        """
        Generate a complete UDS reprogramming session.

        Returns list of (can_id, data, pre_delay_ms) tuples.
        """
        frames = []

        # Phase 1: Enter extended diagnostic session
        self.log("Starting diagnostic session...")
        frames.extend(self._diagnostic_session_control(0x03))  # Extended session

        # Phase 2: Read ECU identification
        self.log("Reading ECU identification...")
        frames.extend(self._read_ecu_identification())

        # Phase 3: Enter programming session
        self.log("Entering programming session...")
        frames.extend(self._diagnostic_session_control(0x02))  # Programming session

        # Phase 4: Security access
        self.log("Performing security access...")
        frames.extend(self._security_access())

        # Phase 5: Disable normal communication
        self.log("Disabling normal communication...")
        frames.extend(self._communication_control())

        # Phase 6: Disable DTC setting
        self.log("Disabling DTC setting...")
        frames.extend(self._control_dtc_setting())

        # Phase 7: Check programming preconditions
        self.log("Checking programming preconditions...")
        frames.extend(self._routine_control_check_preconditions())

        # Phase 8: Erase memory
        self.log("Erasing memory block...")
        frames.extend(self._routine_control_erase_memory())

        # Phase 9: Request download
        self.log("Requesting download...")
        frames.extend(self._request_download())

        # Phase 10: Transfer data blocks
        num_blocks = random.randint(50, 150)
        self.log(f"Transferring {num_blocks} data blocks...")
        frames.extend(self._transfer_data_blocks(num_blocks))

        # Phase 11: Request transfer exit
        self.log("Completing transfer...")
        frames.extend(self._request_transfer_exit())

        # Phase 12: Check programming dependencies
        self.log("Checking programming dependencies...")
        frames.extend(self._routine_control_check_dependencies())

        # Phase 13: Read stored DTCs
        self.log(f"Reading stored DTCs ({len(self.stored_dtcs)} present)...")
        frames.extend(self._read_dtc_info())

        # Phase 14: Clear DTCs
        self.log("Clearing DTCs...")
        frames.extend(self._clear_dtc())

        # Phase 15: Verify DTCs cleared
        self.log("Verifying DTCs cleared...")
        frames.extend(self._read_dtc_count())

        # Phase 16: ECU Reset
        self.log("Resetting ECU...")
        frames.extend(self._ecu_reset())

        # Reset DTCs for next session
        self._reset_dtcs()

        return frames

    def _diagnostic_session_control(self, session_type: int) -> List[Tuple[int, bytes, float]]:
        """Generate Diagnostic Session Control exchange with possible retry."""
        frames = []

        req = make_single_frame(bytes([SID_DIAGNOSTIC_SESSION_CONTROL, session_type]))

        # Occasionally simulate "busy" response requiring retry
        if random.random() < self.ERROR_RATE_BUSY:
            frames.append((self.tester_id, req, random.uniform(20, 50)))
            # Negative response: busy, repeat request
            nrc = negative_response(SID_DIAGNOSTIC_SESSION_CONTROL, NRC_BUSY_REPEAT_REQUEST)
            frames.append((self.ecu_id, nrc, random.uniform(15, 30)))
            self.log("ECU busy, retrying session control...")
            # Retry after delay
            frames.append((self.tester_id, req, random.uniform(100, 200)))

        else:
            frames.append((self.tester_id, req, random.uniform(20, 50)))

        # Positive response
        resp = make_single_frame(bytes([
            positive_response(SID_DIAGNOSTIC_SESSION_CONTROL),
            session_type,
            0x00, 0x19,  # P2 timing (25ms)
            0x01, 0xF4   # P2* timing (5000ms)
        ]))
        frames.append((self.ecu_id, resp, random.uniform(15, 40)))

        return frames

    def _read_ecu_identification(self) -> List[Tuple[int, bytes, float]]:
        """Generate Read Data By Identifier exchanges for ECU info."""
        frames = []

        identifiers = [
            (0xF190, b'WVWZZZ3CZWE123456'),   # VIN
            (0xF18C, b'ECU12345678'),          # ECU Serial Number
            (0xF195, b'SW_V02.15.003'),        # Software Version
            (0xF193, b'HW_REV_C'),             # Hardware Version
            (0xF187, b'PART_1K0907115B'),      # Part Number
        ]

        for did, value in identifiers:
            # Request
            req = make_single_frame(bytes([
                SID_READ_DATA_BY_ID,
                (did >> 8) & 0xFF,
                did & 0xFF
            ]))
            frames.append((self.tester_id, req, random.uniform(30, 80)))

            # Response (may need multi-frame for long data)
            resp_data = bytes([
                positive_response(SID_READ_DATA_BY_ID),
                (did >> 8) & 0xFF,
                did & 0xFF
            ]) + value

            if len(resp_data) <= 7:
                frames.append((self.ecu_id, make_single_frame(resp_data), random.uniform(10, 30)))
            else:
                # Multi-frame response
                frames.append((self.ecu_id, make_first_frame(len(resp_data), resp_data), random.uniform(10, 30)))
                frames.append((self.tester_id, make_flow_control(), random.uniform(5, 15)))

                remaining = resp_data[6:]
                seq = 1
                while remaining:
                    cf = make_consecutive_frame(seq, remaining[:7])
                    frames.append((self.ecu_id, cf, random.uniform(5, 15)))
                    remaining = remaining[7:]
                    seq = (seq + 1) & 0x0F

        return frames

    def _security_access(self) -> List[Tuple[int, bytes, float]]:
        """Generate Security Access exchange (seed & key) with possible failed attempt."""
        frames = []
        security_level = 0x11  # Programming security level

        # Simulate failed first attempt occasionally
        simulate_failure = random.random() < self.ERROR_RATE_SECURITY_FAIL

        if simulate_failure:
            self.log("Simulating failed security access attempt...")

            # First attempt: Request seed
            req_seed = make_single_frame(bytes([SID_SECURITY_ACCESS, security_level]))
            frames.append((self.tester_id, req_seed, random.uniform(20, 40)))

            # Response with seed
            seed1 = bytes([random.randint(0, 255) for _ in range(4)])
            resp_seed = make_single_frame(bytes([
                positive_response(SID_SECURITY_ACCESS),
                security_level
            ]) + seed1)
            frames.append((self.ecu_id, resp_seed, random.uniform(100, 200)))

            # Send WRONG key (simulating calculation error)
            wrong_key = bytes([random.randint(0, 255) for _ in range(4)])
            req_key = make_single_frame(bytes([SID_SECURITY_ACCESS, security_level + 1]) + wrong_key)
            frames.append((self.tester_id, req_key, random.uniform(50, 100)))

            # Negative response: Invalid key
            nrc = negative_response(SID_SECURITY_ACCESS, NRC_INVALID_KEY)
            frames.append((self.ecu_id, nrc, random.uniform(30, 80)))
            self.log("Security access failed (invalid key), retrying...")

            # Wait before retry (required time delay)
            frames.append((0, b'', random.uniform(500, 1000)))  # Delay marker

        # Successful attempt: Request seed
        req_seed = make_single_frame(bytes([SID_SECURITY_ACCESS, security_level]))
        frames.append((self.tester_id, req_seed, random.uniform(20, 40)))

        # Response with seed
        seed = bytes([random.randint(0, 255) for _ in range(4)])
        resp_seed = make_single_frame(bytes([
            positive_response(SID_SECURITY_ACCESS),
            security_level
        ]) + seed)
        frames.append((self.ecu_id, resp_seed, random.uniform(100, 250)))

        # Send correct key
        key = bytes([(b ^ 0xCA) & 0xFF for b in seed])
        req_key = make_single_frame(bytes([SID_SECURITY_ACCESS, security_level + 1]) + key)
        frames.append((self.tester_id, req_key, random.uniform(50, 100)))

        # Key accepted
        resp = make_single_frame(bytes([
            positive_response(SID_SECURITY_ACCESS),
            security_level + 1
        ]))
        frames.append((self.ecu_id, resp, random.uniform(80, 200)))

        return frames

    def _communication_control(self) -> List[Tuple[int, bytes, float]]:
        """Generate Communication Control exchange."""
        frames = []

        # Disable normal message transmission
        req = make_single_frame(bytes([
            SID_COMMUNICATION_CONTROL,
            0x03,  # Disable Rx and Tx
            0x01   # Normal communication messages
        ]))
        frames.append((self.tester_id, req, random.uniform(20, 40)))

        resp = make_single_frame(bytes([
            positive_response(SID_COMMUNICATION_CONTROL),
            0x03
        ]))
        frames.append((self.ecu_id, resp, random.uniform(10, 25)))

        return frames

    def _control_dtc_setting(self) -> List[Tuple[int, bytes, float]]:
        """Generate Control DTC Setting exchange."""
        frames = []

        # Disable DTC setting
        req = make_single_frame(bytes([SID_CONTROL_DTC_SETTING, 0x02]))  # Off
        frames.append((self.tester_id, req, random.uniform(20, 40)))

        resp = make_single_frame(bytes([
            positive_response(SID_CONTROL_DTC_SETTING),
            0x02
        ]))
        frames.append((self.ecu_id, resp, random.uniform(10, 25)))

        return frames

    def _routine_control_check_preconditions(self) -> List[Tuple[int, bytes, float]]:
        """Generate Routine Control - Check Programming Preconditions with possible retry."""
        frames = []
        routine_id = 0xFF00  # Check programming preconditions

        req = make_single_frame(bytes([
            SID_ROUTINE_CONTROL,
            0x01,  # Start routine
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF
        ]))

        # Occasionally fail with "conditions not correct" (e.g., engine running)
        if random.random() < self.ERROR_RATE_CONDITIONS:
            frames.append((self.tester_id, req, random.uniform(30, 60)))

            nrc = negative_response(SID_ROUTINE_CONTROL, NRC_CONDITIONS_NOT_CORRECT)
            frames.append((self.ecu_id, nrc, random.uniform(20, 50)))
            self.log("Preconditions not met, waiting and retrying...")

            # Wait for conditions to be met
            frames.append((0, b'', random.uniform(300, 600)))

            # Retry
            frames.append((self.tester_id, req, random.uniform(30, 60)))
        else:
            frames.append((self.tester_id, req, random.uniform(30, 60)))

        resp = make_single_frame(bytes([
            positive_response(SID_ROUTINE_CONTROL),
            0x01,
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF,
            0x00  # Status: OK
        ]))
        frames.append((self.ecu_id, resp, random.uniform(50, 150)))

        return frames

    def _routine_control_erase_memory(self) -> List[Tuple[int, bytes, float]]:
        """Generate Routine Control - Erase Memory with pending responses."""
        frames = []
        routine_id = 0xFF01  # Erase memory
        memory_address = 0x00080000
        memory_size = 0x00040000

        # Request with memory address and size
        req_data = bytes([
            SID_ROUTINE_CONTROL,
            0x01,  # Start routine
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF,
            0x44,  # Address and length format (4 bytes each)
            (memory_address >> 24) & 0xFF,
            (memory_address >> 16) & 0xFF,
        ])
        frames.append((self.tester_id, make_first_frame(12, req_data), random.uniform(20, 40)))
        frames.append((self.ecu_id, make_flow_control(), random.uniform(5, 15)))

        cf_data = bytes([
            (memory_address >> 8) & 0xFF,
            memory_address & 0xFF,
            (memory_size >> 24) & 0xFF,
            (memory_size >> 16) & 0xFF,
            (memory_size >> 8) & 0xFF,
            memory_size & 0xFF,
        ])
        frames.append((self.tester_id, make_consecutive_frame(1, cf_data), random.uniform(5, 15)))

        # Pending responses during erase (simulates long operation)
        num_pending = random.randint(3, 8)
        for _ in range(num_pending):
            pending = negative_response(SID_ROUTINE_CONTROL, NRC_RESPONSE_PENDING)
            frames.append((self.ecu_id, pending, random.uniform(400, 700)))

        # Final positive response
        resp = make_single_frame(bytes([
            positive_response(SID_ROUTINE_CONTROL),
            0x01,
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF,
            0x00  # Status: completed successfully
        ]))
        frames.append((self.ecu_id, resp, random.uniform(300, 600)))

        return frames

    def _request_download(self) -> List[Tuple[int, bytes, float]]:
        """Generate Request Download exchange."""
        frames = []
        memory_address = 0x00080000
        memory_size = 0x00040000

        # Request
        req_data = bytes([
            SID_REQUEST_DOWNLOAD,
            0x00,  # Data format (no compression, no encryption)
            0x44,  # Address and length format
            (memory_address >> 24) & 0xFF,
            (memory_address >> 16) & 0xFF,
            (memory_address >> 8) & 0xFF,
            memory_address & 0xFF,
        ])
        frames.append((self.tester_id, make_first_frame(11, req_data), random.uniform(20, 40)))
        frames.append((self.ecu_id, make_flow_control(), random.uniform(5, 15)))

        cf_data = bytes([
            (memory_size >> 24) & 0xFF,
            (memory_size >> 16) & 0xFF,
            (memory_size >> 8) & 0xFF,
            memory_size & 0xFF,
        ])
        frames.append((self.tester_id, make_consecutive_frame(1, cf_data), random.uniform(5, 15)))

        # Response with max block length
        max_block_len = 0x0102  # 258 bytes per block
        resp = make_single_frame(bytes([
            positive_response(SID_REQUEST_DOWNLOAD),
            0x20,  # Length format
            (max_block_len >> 8) & 0xFF,
            max_block_len & 0xFF
        ]))
        frames.append((self.ecu_id, resp, random.uniform(30, 80)))

        self.block_counter = 0
        return frames

    def _transfer_data_blocks(self, num_blocks: int) -> List[Tuple[int, bytes, float]]:
        """Generate Transfer Data exchanges for multiple blocks with possible errors."""
        frames = []
        error_injected = False  # Only inject one error per session

        for i in range(num_blocks):
            self.block_counter = (self.block_counter + 1) & 0xFF

            # Generate random "firmware" data
            block_data = bytes([random.randint(0, 255) for _ in range(random.randint(200, 256))])

            # Occasionally inject a transfer error (once per session)
            inject_error = (
                not error_injected
                and i > 5  # Not on first few blocks
                and random.random() < self.ERROR_RATE_TRANSFER_ERROR
            )

            if inject_error:
                error_injected = True
                error_type = random.choice(['sequence', 'programming', 'suspended'])

                if error_type == 'sequence':
                    # Wrong block sequence counter error
                    self.log(f"Simulating wrong block sequence at block {i}...")
                    wrong_counter = (self.block_counter + random.randint(1, 5)) & 0xFF

                    req_data = bytes([SID_TRANSFER_DATA, wrong_counter]) + block_data
                    frames.append((self.tester_id, make_first_frame(len(req_data), req_data), random.uniform(15, 30)))
                    frames.append((self.ecu_id, make_flow_control(0, 5), random.uniform(3, 8)))

                    remaining = req_data[6:]
                    seq = 1
                    while remaining:
                        cf = make_consecutive_frame(seq, remaining[:7])
                        frames.append((self.tester_id, cf, random.uniform(3, 8)))
                        remaining = remaining[7:]
                        seq = (seq + 1) & 0x0F

                    # Negative response: wrong block sequence
                    nrc = negative_response(SID_TRANSFER_DATA, NRC_WRONG_BLOCK_SEQUENCE)
                    frames.append((self.ecu_id, nrc, random.uniform(20, 50)))

                elif error_type == 'programming':
                    # General programming failure (flash write error)
                    self.log(f"Simulating programming failure at block {i}...")

                    req_data = bytes([SID_TRANSFER_DATA, self.block_counter]) + block_data
                    frames.append((self.tester_id, make_first_frame(len(req_data), req_data), random.uniform(15, 30)))
                    frames.append((self.ecu_id, make_flow_control(0, 5), random.uniform(3, 8)))

                    remaining = req_data[6:]
                    seq = 1
                    while remaining:
                        cf = make_consecutive_frame(seq, remaining[:7])
                        frames.append((self.tester_id, cf, random.uniform(3, 8)))
                        remaining = remaining[7:]
                        seq = (seq + 1) & 0x0F

                    # Negative response followed by pending, then success (flash retry)
                    nrc = negative_response(SID_TRANSFER_DATA, NRC_GENERAL_PROGRAMMING_FAILURE)
                    frames.append((self.ecu_id, nrc, random.uniform(50, 100)))

                else:  # suspended
                    # Transfer suspended temporarily
                    self.log(f"Simulating transfer suspended at block {i}...")

                    req_data = bytes([SID_TRANSFER_DATA, self.block_counter]) + block_data
                    frames.append((self.tester_id, make_first_frame(len(req_data), req_data), random.uniform(15, 30)))
                    frames.append((self.ecu_id, make_flow_control(0, 5), random.uniform(3, 8)))

                    remaining = req_data[6:]
                    seq = 1
                    while remaining:
                        cf = make_consecutive_frame(seq, remaining[:7])
                        frames.append((self.tester_id, cf, random.uniform(3, 8)))
                        remaining = remaining[7:]
                        seq = (seq + 1) & 0x0F

                    nrc = negative_response(SID_TRANSFER_DATA, NRC_TRANSFER_DATA_SUSPENDED)
                    frames.append((self.ecu_id, nrc, random.uniform(30, 60)))

                # Retry the block
                self.log("Retrying block transfer...")

            # Normal or retry transfer
            req_data = bytes([SID_TRANSFER_DATA, self.block_counter]) + block_data

            # First frame
            frames.append((self.tester_id, make_first_frame(len(req_data), req_data), random.uniform(15, 30)))
            frames.append((self.ecu_id, make_flow_control(0, 5), random.uniform(3, 8)))

            # Consecutive frames
            remaining = req_data[6:]
            seq = 1
            while remaining:
                cf = make_consecutive_frame(seq, remaining[:7])
                frames.append((self.tester_id, cf, random.uniform(3, 8)))
                remaining = remaining[7:]
                seq = (seq + 1) & 0x0F

            # Response
            resp = make_single_frame(bytes([
                positive_response(SID_TRANSFER_DATA),
                self.block_counter
            ]))
            frames.append((self.ecu_id, resp, random.uniform(10, 25)))

            # Occasional pending responses to simulate slower blocks
            if random.random() < 0.05:
                pending = negative_response(SID_TRANSFER_DATA, NRC_RESPONSE_PENDING)
                # Insert before the positive response
                frames.insert(-1, (self.ecu_id, pending, random.uniform(100, 200)))

        return frames

    def _request_transfer_exit(self) -> List[Tuple[int, bytes, float]]:
        """Generate Request Transfer Exit exchange."""
        frames = []

        req = make_single_frame(bytes([SID_REQUEST_TRANSFER_EXIT]))
        frames.append((self.tester_id, req, random.uniform(20, 40)))

        resp = make_single_frame(bytes([positive_response(SID_REQUEST_TRANSFER_EXIT)]))
        frames.append((self.ecu_id, resp, random.uniform(30, 80)))

        return frames

    def _routine_control_check_dependencies(self) -> List[Tuple[int, bytes, float]]:
        """Generate Routine Control - Check Programming Dependencies."""
        frames = []
        routine_id = 0xFF02  # Check dependencies

        req = make_single_frame(bytes([
            SID_ROUTINE_CONTROL,
            0x01,
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF
        ]))
        frames.append((self.tester_id, req, random.uniform(30, 60)))

        # Pending response during check
        pending = negative_response(SID_ROUTINE_CONTROL, NRC_RESPONSE_PENDING)
        frames.append((self.ecu_id, pending, random.uniform(200, 400)))

        resp = make_single_frame(bytes([
            positive_response(SID_ROUTINE_CONTROL),
            0x01,
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF,
            0x00
        ]))
        frames.append((self.ecu_id, resp, random.uniform(100, 300)))

        return frames

    def _read_dtc_info(self) -> List[Tuple[int, bytes, float]]:
        """Generate Read DTC Information exchanges."""
        frames = []

        # First: Report number of DTCs by status mask
        frames.extend(self._read_dtc_count())

        if not self.stored_dtcs:
            return frames

        # Second: Report DTCs by status mask (get actual DTC codes)
        status_mask = 0xFF  # All status bits
        req = make_single_frame(bytes([
            SID_READ_DTC_INFO,
            DTC_REPORT_BY_STATUS_MASK,
            status_mask
        ]))
        frames.append((self.tester_id, req, random.uniform(30, 60)))

        # Build response with all DTCs
        # Response format: [SID+0x40, sub-func, status_availability_mask, DTC1_hi, DTC1_mid, DTC1_lo, DTC1_status, ...]
        resp_data = bytes([
            positive_response(SID_READ_DTC_INFO),
            DTC_REPORT_BY_STATUS_MASK,
            0xFF  # Status availability mask
        ])

        for dtc_code, dtc_status, _ in self.stored_dtcs:
            # DTC is 2 bytes, but UDS format uses 3 bytes (high, mid, low)
            dtc_hi = (dtc_code >> 8) & 0xFF
            dtc_mid = dtc_code & 0xFF
            dtc_lo = 0x00  # Failure type byte (simplified)
            resp_data += bytes([dtc_hi, dtc_mid, dtc_lo, dtc_status])

        # Multi-frame response needed for multiple DTCs
        if len(resp_data) <= 7:
            frames.append((self.ecu_id, make_single_frame(resp_data), random.uniform(20, 50)))
        else:
            frames.append((self.ecu_id, make_first_frame(len(resp_data), resp_data), random.uniform(20, 50)))
            frames.append((self.tester_id, make_flow_control(), random.uniform(5, 15)))

            remaining = resp_data[6:]
            seq = 1
            while remaining:
                cf = make_consecutive_frame(seq, remaining[:7])
                frames.append((self.ecu_id, cf, random.uniform(5, 15)))
                remaining = remaining[7:]
                seq = (seq + 1) & 0x0F

        # Optionally read snapshot data for first DTC
        if self.stored_dtcs and random.random() < 0.5:
            frames.extend(self._read_dtc_snapshot(self.stored_dtcs[0]))

        return frames

    def _read_dtc_count(self) -> List[Tuple[int, bytes, float]]:
        """Generate Read DTC count exchange."""
        frames = []

        status_mask = 0xFF  # All status bits
        req = make_single_frame(bytes([
            SID_READ_DTC_INFO,
            DTC_REPORT_NUMBER_BY_STATUS_MASK,
            status_mask
        ]))
        frames.append((self.tester_id, req, random.uniform(30, 60)))

        # Response: [SID+0x40, sub-func, status_availability, format, count_hi, count_lo]
        dtc_count = len(self.stored_dtcs)
        resp = make_single_frame(bytes([
            positive_response(SID_READ_DTC_INFO),
            DTC_REPORT_NUMBER_BY_STATUS_MASK,
            0xFF,  # Status availability mask
            0x01,  # DTC format identifier (ISO 14229-1)
            (dtc_count >> 8) & 0xFF,
            dtc_count & 0xFF
        ]))
        frames.append((self.ecu_id, resp, random.uniform(15, 40)))

        return frames

    def _read_dtc_snapshot(self, dtc: Tuple[int, int, str]) -> List[Tuple[int, bytes, float]]:
        """Generate Read DTC Snapshot exchange for a specific DTC."""
        frames = []
        dtc_code, dtc_status, _ = dtc

        dtc_hi = (dtc_code >> 8) & 0xFF
        dtc_mid = dtc_code & 0xFF
        dtc_lo = 0x00

        # Request snapshot record
        req = make_single_frame(bytes([
            SID_READ_DTC_INFO,
            DTC_REPORT_SNAPSHOT_BY_DTC,
            dtc_hi, dtc_mid, dtc_lo,
            0xFF  # Record number (all records)
        ]))
        frames.append((self.tester_id, req, random.uniform(30, 60)))

        # Generate fake snapshot data
        # Snapshot typically contains freeze frame data captured when DTC was set
        snapshot_data = bytes([
            positive_response(SID_READ_DTC_INFO),
            DTC_REPORT_SNAPSHOT_BY_DTC,
            dtc_hi, dtc_mid, dtc_lo,
            0x01,  # Status record
            0x01,  # Snapshot record number
            0x02,  # Number of identifiers
            # Freeze frame data: RPM at time of fault
            0x0C,  # PID for RPM (high byte)
            0x0C, random.randint(0x08, 0x20), random.randint(0, 255),
            # Freeze frame data: Coolant temp at time of fault
            0x05,  # PID for coolant temp
            random.randint(0x50, 0x90),  # 40-104°C encoded
        ])

        if len(snapshot_data) <= 7:
            frames.append((self.ecu_id, make_single_frame(snapshot_data), random.uniform(20, 50)))
        else:
            frames.append((self.ecu_id, make_first_frame(len(snapshot_data), snapshot_data), random.uniform(20, 50)))
            frames.append((self.tester_id, make_flow_control(), random.uniform(5, 15)))

            remaining = snapshot_data[6:]
            seq = 1
            while remaining:
                cf = make_consecutive_frame(seq, remaining[:7])
                frames.append((self.ecu_id, cf, random.uniform(5, 15)))
                remaining = remaining[7:]
                seq = (seq + 1) & 0x0F

        return frames

    def _clear_dtc(self) -> List[Tuple[int, bytes, float]]:
        """Generate Clear Diagnostic Information exchange."""
        frames = []

        # Clear all DTCs (group 0xFFFFFF)
        req = make_single_frame(bytes([SID_CLEAR_DTC, 0xFF, 0xFF, 0xFF]))
        frames.append((self.tester_id, req, random.uniform(30, 60)))

        # Pending response
        pending = negative_response(SID_CLEAR_DTC, NRC_RESPONSE_PENDING)
        frames.append((self.ecu_id, pending, random.uniform(150, 300)))

        resp = make_single_frame(bytes([positive_response(SID_CLEAR_DTC)]))
        frames.append((self.ecu_id, resp, random.uniform(100, 250)))

        # Clear stored DTCs
        self.stored_dtcs = []

        return frames

    def _ecu_reset(self) -> List[Tuple[int, bytes, float]]:
        """Generate ECU Reset exchange."""
        frames = []

        req = make_single_frame(bytes([SID_ECU_RESET, 0x01]))  # Hard reset
        frames.append((self.tester_id, req, random.uniform(30, 60)))

        resp = make_single_frame(bytes([
            positive_response(SID_ECU_RESET),
            0x01
        ]))
        frames.append((self.ecu_id, resp, random.uniform(20, 50)))

        # Simulate ECU being offline during reset (no frames for ~1.5s)
        # This is represented by a long delay on the "virtual" next frame

        return frames


class OBDIISimulator:
    """Generates realistic OBD-II polling traffic."""

    # OBD-II uses 2-byte DTC format (different from UDS 3-byte)
    OBD_DTCS = [
        (0x0300, "P0300 - Random Misfire"),
        (0x0171, "P0171 - System Too Lean"),
        (0x0420, "P0420 - Catalyst Efficiency"),
        (0x0442, "P0442 - EVAP Leak"),
        (0x0135, "P0135 - O2 Sensor Heater"),
    ]

    def __init__(
        self,
        speed: float = 1.0,
        verbose: bool = False
    ):
        self.speed = speed
        self.verbose = verbose
        self.request_id = OBD_FUNCTIONAL_REQUEST_ID
        self.response_id = OBD_RESPONSE_ID_BASE

        # Simulated vehicle state
        self.engine_running = True
        self.coolant_temp = 85  # °C (normal operating temp)
        self.rpm = 800  # idle
        self.vehicle_speed = 0  # km/h
        self.throttle = 15  # %
        self.intake_temp = 25  # °C
        self.maf_flow = 3.5  # g/s at idle
        self.fuel_level = 65  # %
        self.ambient_temp = 22  # °C
        self.runtime_seconds = 0

        # Stored DTCs (randomized subset)
        self._reset_dtcs()

    def _reset_dtcs(self):
        """Reset DTCs to a random subset."""
        all_dtcs = list(self.OBD_DTCS)
        random.shuffle(all_dtcs)
        num_dtcs = random.randint(1, 3)
        self.stored_dtcs = all_dtcs[:num_dtcs]
        self.pending_dtcs = [all_dtcs[num_dtcs]] if num_dtcs < len(all_dtcs) else []

    def log(self, msg: str):
        if self.verbose:
            print(f"{ts()} {color('[obd]', '33')} {msg}")

    def _simulate_driving(self):
        """Update vehicle state to simulate some driving activity."""
        # Random fluctuations to simulate real sensor data
        self.coolant_temp = max(70, min(105, self.coolant_temp + random.uniform(-1, 1)))
        self.intake_temp = max(15, min(45, self.intake_temp + random.uniform(-0.5, 0.5)))
        self.ambient_temp = max(15, min(35, self.ambient_temp + random.uniform(-0.2, 0.2)))

        # Simulate occasional acceleration/deceleration
        if random.random() < 0.1:
            # Accelerating
            self.throttle = min(80, self.throttle + random.uniform(10, 30))
            self.rpm = min(4500, self.rpm + random.uniform(200, 800))
            self.vehicle_speed = min(120, self.vehicle_speed + random.uniform(5, 15))
            self.maf_flow = min(50, self.maf_flow + random.uniform(2, 10))
        elif random.random() < 0.15:
            # Decelerating
            self.throttle = max(12, self.throttle - random.uniform(5, 20))
            self.rpm = max(700, self.rpm - random.uniform(100, 400))
            self.vehicle_speed = max(0, self.vehicle_speed - random.uniform(3, 10))
            self.maf_flow = max(2, self.maf_flow - random.uniform(1, 5))
        else:
            # Cruising / idle fluctuations
            self.throttle = max(12, min(25, self.throttle + random.uniform(-2, 2)))
            self.rpm = max(650, min(1200, self.rpm + random.uniform(-50, 50)))
            self.maf_flow = max(2, min(8, self.maf_flow + random.uniform(-0.5, 0.5)))

        self.fuel_level = max(0, self.fuel_level - random.uniform(0, 0.01))
        self.runtime_seconds += random.randint(50, 150)

    def generate_obd_polling(self, num_cycles: int = 10) -> List[Tuple[int, bytes, float]]:
        """
        Generate OBD-II polling cycles.

        Each cycle polls multiple PIDs like a typical scan tool would.
        Returns list of (can_id, data, pre_delay_ms) tuples.
        """
        frames = []

        pids_to_poll = [
            OBD_PID_ENGINE_RPM,
            OBD_PID_VEHICLE_SPEED,
            OBD_PID_ENGINE_COOLANT_TEMP,
            OBD_PID_THROTTLE_POSITION,
            OBD_PID_INTAKE_AIR_TEMP,
            OBD_PID_MAF_FLOW,
            OBD_PID_FUEL_LEVEL,
            OBD_PID_RUNTIME,
            OBD_PID_AMBIENT_TEMP,
        ]

        for cycle in range(num_cycles):
            self._simulate_driving()

            # Occasionally poll supported PIDs
            if cycle == 0 or random.random() < 0.05:
                frames.extend(self._poll_supported_pids())

            # Poll stored DTCs occasionally (every few cycles)
            if cycle == 0 or random.random() < 0.1:
                frames.extend(self._poll_stored_dtcs())

            # Poll pending DTCs occasionally
            if random.random() < 0.05:
                frames.extend(self._poll_pending_dtcs())

            # Poll a subset of PIDs each cycle (like a real scan tool)
            pids_this_cycle = random.sample(pids_to_poll, k=random.randint(3, 6))

            for pid in pids_this_cycle:
                frames.extend(self._poll_pid(pid))

            # Inter-cycle delay (scan tools typically poll at 5-10 Hz)
            frames.append((0, b'', random.uniform(80, 150)))  # Marker for delay only

        return frames

    def _poll_supported_pids(self) -> List[Tuple[int, bytes, float]]:
        """Poll supported PIDs (Service 01, PID 00)."""
        frames = []

        # Request
        req = bytes([0x02, 0x01, OBD_PID_SUPPORTED_01_20, 0x00, 0x00, 0x00, 0x00, 0x00])
        frames.append((self.request_id, req, random.uniform(20, 40)))

        # Response - supported PIDs bitmap
        # Bits set for: 05, 0C, 0D, 0F, 10, 11, 1F, 2F
        supported = 0b00011000_00011111_00000001_00000001
        resp = bytes([
            0x06, 0x41, OBD_PID_SUPPORTED_01_20,
            (supported >> 24) & 0xFF,
            (supported >> 16) & 0xFF,
            (supported >> 8) & 0xFF,
            supported & 0xFF,
            0x00
        ])
        frames.append((self.response_id, resp, random.uniform(5, 20)))

        return frames

    def _poll_pid(self, pid: int) -> List[Tuple[int, bytes, float]]:
        """Poll a specific OBD-II PID and generate response."""
        frames = []

        # Request (functional addressing)
        use_functional = random.random() < 0.8
        req_id = self.request_id if use_functional else OBD_PHYSICAL_REQUEST_ID

        req = bytes([0x02, 0x01, pid, 0x00, 0x00, 0x00, 0x00, 0x00])
        frames.append((req_id, req, random.uniform(15, 35)))

        # Generate response based on PID
        resp_data = self._generate_pid_response(pid)
        if resp_data:
            frames.append((self.response_id, resp_data, random.uniform(5, 25)))

        return frames

    def _generate_pid_response(self, pid: int) -> bytes:
        """Generate a response for a specific PID."""
        if pid == OBD_PID_ENGINE_COOLANT_TEMP:
            # Value = A - 40 (range: -40 to 215°C)
            value = int(self.coolant_temp) + 40
            return bytes([0x03, 0x41, pid, value, 0x00, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_ENGINE_RPM:
            # Value = ((A * 256) + B) / 4
            rpm_encoded = int(self.rpm * 4)
            return bytes([0x04, 0x41, pid, (rpm_encoded >> 8) & 0xFF, rpm_encoded & 0xFF, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_VEHICLE_SPEED:
            # Value = A (0-255 km/h)
            return bytes([0x03, 0x41, pid, int(self.vehicle_speed), 0x00, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_THROTTLE_POSITION:
            # Value = A * 100 / 255
            value = int(self.throttle * 255 / 100)
            return bytes([0x03, 0x41, pid, value, 0x00, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_INTAKE_AIR_TEMP:
            # Value = A - 40
            value = int(self.intake_temp) + 40
            return bytes([0x03, 0x41, pid, value, 0x00, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_MAF_FLOW:
            # Value = ((A * 256) + B) / 100 (g/s)
            maf_encoded = int(self.maf_flow * 100)
            return bytes([0x04, 0x41, pid, (maf_encoded >> 8) & 0xFF, maf_encoded & 0xFF, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_FUEL_LEVEL:
            # Value = A * 100 / 255
            value = int(self.fuel_level * 255 / 100)
            return bytes([0x03, 0x41, pid, value, 0x00, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_RUNTIME:
            # Value = (A * 256) + B (seconds)
            return bytes([0x04, 0x41, pid, (self.runtime_seconds >> 8) & 0xFF, self.runtime_seconds & 0xFF, 0x00, 0x00, 0x00])

        elif pid == OBD_PID_AMBIENT_TEMP:
            # Value = A - 40
            value = int(self.ambient_temp) + 40
            return bytes([0x03, 0x41, pid, value, 0x00, 0x00, 0x00, 0x00])

        return bytes([0x03, 0x7F, 0x01, 0x12, 0x00, 0x00, 0x00, 0x00])  # Service not supported

    def _poll_stored_dtcs(self) -> List[Tuple[int, bytes, float]]:
        """Poll stored DTCs using OBD-II Service 03."""
        frames = []

        # Request stored DTCs
        req = bytes([0x01, OBD_SID_SHOW_STORED_DTCS, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        frames.append((self.request_id, req, random.uniform(20, 40)))

        # Response format: [num_bytes, 0x43, num_dtcs, DTC1_hi, DTC1_lo, DTC2_hi, DTC2_lo, ...]
        num_dtcs = len(self.stored_dtcs)

        if num_dtcs == 0:
            # No DTCs
            resp = bytes([0x02, 0x43, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        elif num_dtcs <= 2:
            # Fits in single frame (up to 2 DTCs)
            resp_data = [0x43, num_dtcs]
            for dtc_code, _ in self.stored_dtcs[:2]:
                resp_data.extend([(dtc_code >> 8) & 0xFF, dtc_code & 0xFF])
            # Pad to 8 bytes
            resp_len = 2 + num_dtcs * 2
            resp = bytes([resp_len] + resp_data + [0x00] * (7 - len(resp_data)))
        else:
            # Multi-frame response needed for 3+ DTCs
            resp_data = bytes([0x43, num_dtcs])
            for dtc_code, _ in self.stored_dtcs:
                resp_data += bytes([(dtc_code >> 8) & 0xFF, dtc_code & 0xFF])

            # First frame
            total_len = len(resp_data)
            ff = bytes([0x10 | ((total_len >> 8) & 0x0F), total_len & 0xFF]) + resp_data[:6]
            frames.append((self.response_id, ff, random.uniform(10, 25)))

            # Flow control from tester
            frames.append((self.request_id, bytes([0x30, 0x00, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00]), random.uniform(5, 15)))

            # Consecutive frames
            remaining = resp_data[6:]
            seq = 1
            while remaining:
                cf = bytes([0x20 | (seq & 0x0F)]) + remaining[:7]
                cf = cf.ljust(8, b'\x00')
                frames.append((self.response_id, cf, random.uniform(5, 15)))
                remaining = remaining[7:]
                seq = (seq + 1) & 0x0F

            return frames

        frames.append((self.response_id, resp, random.uniform(10, 30)))
        return frames

    def _poll_pending_dtcs(self) -> List[Tuple[int, bytes, float]]:
        """Poll pending DTCs using OBD-II Service 07."""
        frames = []

        # Request pending DTCs
        req = bytes([0x01, OBD_SID_SHOW_PENDING_DTCS, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        frames.append((self.request_id, req, random.uniform(20, 40)))

        # Response
        num_dtcs = len(self.pending_dtcs)

        if num_dtcs == 0:
            resp = bytes([0x02, 0x47, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        else:
            resp_data = [0x47, num_dtcs]
            for dtc_code, _ in self.pending_dtcs[:2]:
                resp_data.extend([(dtc_code >> 8) & 0xFF, dtc_code & 0xFF])
            resp_len = 2 + min(num_dtcs, 2) * 2
            resp = bytes([resp_len] + resp_data + [0x00] * (7 - len(resp_data)))

        frames.append((self.response_id, resp, random.uniform(10, 30)))
        return frames


def send_frames(
    frames: List[Tuple[int, bytes, float]],
    client_manager: ClientManager,
    speed: float,
    stop_event: threading.Event,
    verbose: bool,
    frame_count: int,
    tester_id: int = 0x7E0
) -> int:
    """
    Send a list of frames with timing delays.

    Returns the updated frame count.
    """
    for can_id, data, delay_ms in frames:
        if stop_event.is_set():
            break

        # Apply delay (scaled by speed)
        scaled_delay = max(0.001, delay_ms / 1000.0 / speed)
        time.sleep(scaled_delay)

        # Skip delay-only markers (can_id=0)
        if can_id == 0:
            continue

        clients = client_manager.client_count()
        if clients == 0:
            continue

        # Create frame packet
        ts_us = int(time.time() * 1_000_000)
        packet = pack_frame(ts_us, can_id, False, data)

        frame_count += 1
        client_manager.broadcast(packet)

        if verbose:
            # Determine direction based on ID
            if can_id == OBD_FUNCTIONAL_REQUEST_ID or can_id == tester_id:
                direction = "TX"
            else:
                direction = "RX"
            data_hex = data.hex().upper()
            print(
                f"{ts()} {color('[can]', '36')} "
                f"FWD #{frame_count:5d} {direction} ID={can_id:03X} "
                f"DLC={len(data)} DATA={data_hex} -> {clients} client(s)"
            )

    return frame_count


def uds_generator_thread(
    session: UDSSession,
    obd_sim: OBDIISimulator,
    client_manager: ClientManager,
    stop_event: threading.Event,
    loop: bool,
    verbose: bool,
    sync_mode: bool
):
    """Thread that generates UDS and OBD-II frames and broadcasts to clients."""
    frame_count = 0
    session_count = 0

    while not stop_event.is_set():
        # In sync mode, wait for at least one client before generating
        if sync_mode:
            while not stop_event.is_set() and client_manager.client_count() == 0:
                time.sleep(0.1)
            
            if stop_event.is_set():
                break
                
            print(f"{ts()} {color('[sync]', '35')} Client connected, starting UDS session...")

        session_count += 1
        print(f"{ts()} {color('[uds]', '32')} Starting UDS session #{session_count}")

        # Generate and send UDS session frames
        uds_frames = session.generate_session()
        frame_count = send_frames(
            uds_frames, client_manager, session.speed, stop_event,
            verbose, frame_count, session.tester_id
        )

        if not loop:
            print(f"{ts()} {color('[uds]', '32')} Session complete. Waiting for next client connection...")
            
            # In sync mode with no-loop, wait for current client to disconnect
            if sync_mode and client_manager.client_count() > 0:
                print(f"{ts()} {color('[sync]', '35')} Waiting for current client to disconnect...")
                while not stop_event.is_set() and client_manager.client_count() > 0:
                    time.sleep(0.1)
                    
            continue

        if stop_event.is_set():
            break

        # Check if we should continue (in sync mode, stop if no clients)
        if sync_mode and client_manager.client_count() == 0:
            print(f"{ts()} {color('[sync]', '35')} No clients connected, pausing generation...")
            continue

        # Generate OBD-II traffic between sessions
        obd_cycles = random.randint(15, 30)
        print(f"{ts()} {color('[obd]', '33')} Generating {obd_cycles} OBD-II polling cycles...")

        obd_frames = obd_sim.generate_obd_polling(num_cycles=obd_cycles)
        frame_count = send_frames(
            obd_frames, client_manager, session.speed, stop_event,
            verbose, frame_count, session.tester_id
        )

        if stop_event.is_set():
            break

        # Short delay before next UDS session
        restart_delay = random.uniform(1.0, 2.0) / session.speed
        print(f"{ts()} {color('[uds]', '33')} Next UDS session in {restart_delay:.1f}s...")

        for _ in range(int(restart_delay * 10)):
            if stop_event.is_set():
                break
            # In sync mode, also check if clients disconnected during delay
            if sync_mode and client_manager.client_count() == 0:
                break
            time.sleep(0.1)

    print(f"{ts()} {color('[uds]', '32')} Generator stopped. Total frames: {frame_count}")


def main():
    parser = argparse.ArgumentParser(
        description=HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="TCP server port (default: auto-pick >=42420)."
    )
    parser.add_argument(
        "--tester-id",
        type=lambda x: int(x, 0),
        default=0x7E0,
        metavar="ID",
        help="CAN ID for tester/diagnostic tool requests (default: 0x7E0)."
    )
    parser.add_argument(
        "--ecu-id",
        type=lambda x: int(x, 0),
        default=0x7E8,
        metavar="ID",
        help="CAN ID for ECU responses (default: 0x7E8)."
    )
    parser.add_argument(
        "--speed", "-s",
        type=float,
        default=1.0,
        metavar="FACTOR",
        help="Speed multiplier for timing (default: 1.0). "
             "Use 2.0 for double speed, 0.5 for half speed."
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each CAN frame when it is forwarded to TCP clients."
    )
    parser.add_argument(
        "--no-loop",
        action="store_true",
        help="Run single session per client connection instead of continuous looping."
    )
    parser.add_argument(
        "--service-name",
        type=str,
        default=None,
        metavar="NAME",
        help="Custom Zeroconf service name."
    )
    parser.add_argument(
        "--no-zeroconf",
        action="store_true",
        help="Disable Zeroconf service advertisement."
    )
    parser.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help="Run in async mode (free-running). Default is sync mode where "
             "UDS sessions only start when a client connects."
    )

    args = parser.parse_args()

    requested_port = args.port
    if requested_port is None:
        try:
            listen_port = find_available_port()
        except RuntimeError as e:
            print(f"{ts()} {color('[error]', '31')} {e}")
            return 1
    else:
        listen_port = requested_port

    port_label = f"{listen_port}" if requested_port is not None else f"{listen_port} (auto)"

    sync_mode = not args.async_mode

    print(f"{ts()} {color('[init]', '34')} Mock UDS-on-CAN Logger starting...")
    print(f"{ts()} {color('[init]', '34')} TCP port: {port_label}")
    print(f"{ts()} {color('[init]', '34')} Tester ID: 0x{args.tester_id:03X}, ECU ID: 0x{args.ecu_id:03X}")
    print(f"{ts()} {color('[init]', '34')} Speed: {args.speed}x, Loop: {not args.no_loop}")
    print(f"{ts()} {color('[init]', '34')} Mode: {'sync' if sync_mode else 'async'}")

    client_manager = ClientManager()
    stop_event = threading.Event()

    session = UDSSession(
        tester_id=args.tester_id,
        ecu_id=args.ecu_id,
        speed=args.speed,
        verbose=args.verbose
    )

    obd_sim = OBDIISimulator(
        speed=args.speed,
        verbose=args.verbose
    )

    server_thread = threading.Thread(
        target=tcp_server_thread,
        args=(listen_port, client_manager, stop_event),
        daemon=True
    )
    server_thread.start()

    uds_thread = threading.Thread(
        target=uds_generator_thread,
        args=(session, obd_sim, client_manager, stop_event, not args.no_loop, args.verbose, sync_mode),
        daemon=True
    )
    uds_thread.start()

    print(f"{ts()} {color('[ready]', '32')} Simulator ready. Clients can connect to port {listen_port}")
    if sync_mode:
        print(f"{ts()} {color('[ready]', '32')} Running in sync mode - UDS sessions will start when a client connects")
    else:
        print(f"{ts()} {color('[ready]', '32')} Running in async mode - UDS sessions are running continuously")
    print(f"{ts()} {color('[ready]', '32')} Press Ctrl+C to stop")

    zeroconf_service = None
    if not args.no_zeroconf:
        # Use short hostname to avoid "Too long" errors with FQDNs
        hostname = socket.gethostname().split('.')[0]
        default_name = args.service_name or f"{SERVICE_NAME_PREFIX} Mock-UDS {hostname}:{listen_port}"
        metadata = {
            "process": Path(__file__).name,
            "interface": "mock-uds",
            "tester_id": f"0x{args.tester_id:03X}",
            "ecu_id": f"0x{args.ecu_id:03X}",
        }
        zeroconf_service = LoggerZeroconfService(
            port=listen_port,
            service_name=default_name,
            properties=metadata,
            logger=zeroconf_log,
        )
        zeroconf_service.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{ts()} {color('[exit]', '33')} Shutting down...")
        stop_event.set()

    stats = client_manager.stats
    print(
        f"{ts()} {color('[stats]', '34')} "
        f"Frames sent: {stats['frames_sent']}, "
        f"dropped: {stats['frames_dropped']}, "
        f"bytes: {stats['bytes_sent']}"
    )

    if zeroconf_service:
        zeroconf_service.stop()

    client_manager.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
