# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CANcorder-utils is a Python-based collection of logger proxy utilities for the CANcorder professional CAN bus logger and analyzer. These utilities bridge various CAN hardware adapters with the CANcorder application by implementing the ECUconnect Logger binary protocol over TCP.

### Key Capabilities
- Universal CAN interface support through python-can library
- Native Rusoku TouCAN adapter support via ctypes
- Multi-client TCP streaming with real-time CAN frame forwarding
- Optional mDNS/Zeroconf service discovery

## Common Development Commands

### Installation and Setup
```bash
# Core dependencies for canlogger.py
pip3 install python-can

# Optional interface-specific dependencies
pip3 install gs_usb          # For CANable, candleLight adapters
pip3 install pyserial         # For serial/SLCAN interfaces
pip3 install zeroconf         # For service discovery

# For Rusoku TouCAN support (macOS only)
cd RusokuCAN.dylib
./build_no.sh
make all
sudo make install
```

### Running the Utilities

```bash
# Linux with SocketCAN
sudo ip link set can0 up type can bitrate 500000
python3 utils/canlogger.py --interface socketcan --channel can0

# macOS/Linux with gs_usb devices
sudo python3 utils/canlogger.py --interface gs_usb --channel 0

# Auto-detect interface (recommended for development)
sudo python3 utils/canlogger.py --auto -v

# Rusoku TouCAN (macOS)
python3 utils/rusoku_canlogger.py --channel 0 --bitrate 500000
```

### Testing and Development

```bash
# Virtual CAN for testing (Linux only)
sudo modprobe vcan
sudo ip link add vcan0 type vcan
sudo ip link set vcan0 up
python3 utils/canlogger.py --interface socketcan --channel vcan0

# Mock UDS traffic (no hardware required)
python3 utils/mock_uds_canlogger.py -v

# Monitor raw protocol output
nc localhost <PORT> | xxd

# Test service discovery
dns-sd -B _ecuconnect-log._tcp
```

### Mock UDS Simulator

The `mock_uds_canlogger.py` generates synthetic automotive diagnostic traffic for testing without hardware:

```bash
# Basic usage - continuous UDS reprogramming sessions with OBD-II polling
python3 utils/mock_uds_canlogger.py

# Verbose with faster timing
python3 utils/mock_uds_canlogger.py -v --speed 2.0

# Single session
python3 utils/mock_uds_canlogger.py --no-loop
```

**Simulated UDS Services:**
- 0x10 DiagnosticSessionControl, 0x27 SecurityAccess, 0x22 ReadDataByIdentifier
- 0x19 ReadDTCInformation, 0x14 ClearDTC, 0x31 RoutineControl
- 0x34 RequestDownload, 0x36 TransferData, 0x37 RequestTransferExit
- 0x11 ECUReset, 0x28 CommunicationControl, 0x85 ControlDTCSetting

**Simulated OBD-II Services:**
- Service 01 (current data): RPM, speed, temps, throttle, fuel level
- Service 03 (stored DTCs), Service 07 (pending DTCs)

**Error Simulation:**
- NRC 0x21 (Busy), 0x22 (Conditions Not Correct), 0x35 (Invalid Key)
- NRC 0x71-0x73 (Transfer errors) with automatic retries

## Architecture Overview

### Binary Protocol Format
The ECUconnect Logger protocol uses a fixed-header binary format for each CAN frame:
```
[timestamp:8 bytes, uint64_t, big-endian, microseconds since epoch]
[can_id:4 bytes, uint32_t, big-endian]
[ext:1 byte, 0=standard, 1=extended]
[dlc:1 byte, data length]
[data:0-64 bytes]
```
Total: 14 + dlc bytes per frame

### Module Structure
```
utils/
├── canlogger.py            # Universal adapter using python-can
├── rusoku_canlogger.py     # Native Rusoku TouCAN adapter
├── mock_uds_canlogger.py   # UDS/OBD-II traffic simulator (no hardware required)
├── canlogger_common.py     # Shared protocol implementation and TCP server
└── zeroconf_service.py     # mDNS service advertisement
```

### Threading Model
- **Main thread**: Lifecycle management and signal handling
- **TCP server thread**: Accepts incoming client connections
- **Client handler threads**: One thread per connected client
- **CAN receiver thread**: Reads frames from hardware and broadcasts to clients

### Key Design Patterns
1. **Adapter Pattern**: Different loggers adapt various CAN interfaces to the common TCP protocol
2. **Observer Pattern**: ClientManager broadcasts frames to all connected clients
3. **Template Method**: Common base functionality with hardware-specific implementations

## Important Implementation Notes

### Adding New CAN Interface Support
1. Create a new logger script following the pattern of existing adapters
2. Import and use `canlogger_common` for protocol implementation
3. Implement hardware-specific initialization and frame reading
4. Call `setup_can_receiver()` with your CAN bus object

### Service Discovery
When Zeroconf is available, services are advertised as:
- Service type: `_ecuconnect-log._tcp.local.`
- Properties include: bitrate, interface type, channel, and version

### Error Handling
- The utilities filter out error frames and remote frames
- Client disconnections are handled gracefully without affecting other clients
- Hardware errors cause immediate termination with descriptive messages

### Platform-Specific Considerations
- **Linux**: Requires CAP_NET_ADMIN or sudo for SocketCAN
- **macOS**: May require sudo for USB device access
- **Windows**: Untested but should work with appropriate python-can drivers

## Development Best Practices

### Testing Changes
1. Always test with virtual CAN (vcan0) on Linux when possible
2. Verify protocol compatibility by monitoring with `nc localhost <port> | xxd`
3. Test multi-client scenarios to ensure broadcast functionality
4. Check service discovery with `dns-sd` or `avahi-browse`

### Code Style
- Follow existing patterns in canlogger_common.py for consistency
- Use descriptive logging with appropriate verbosity levels
- Handle keyboard interrupts gracefully for clean shutdown
- Validate CAN hardware availability before starting TCP server