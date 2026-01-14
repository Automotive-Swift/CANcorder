# CANcorder Utilities

Logger proxy utilities for the [CANcorder](../CANcorder) professional CAN bus logger and analyzer.

## Overview

CANcorder is a professional CAN bus logger and analyzer built with modern web technologies (Tauri 2, React 19, Rust) that provides real-time CAN frame capture, protocol analysis, and logging capabilities. While CANcorder connects to CAN hardware via TCP for maximum flexibility, most CAN adapters don't natively speak the ECUconnect Logger TCP protocol that CANcorder expects.

This utilities collection bridges that gap by providing **logger proxies** that:
- Connect to common CAN adapters using their native interfaces
- Stream CAN frames to CANcorder via TCP in the expected binary format
- Enable CANcorder's real-time logging features with any supported CAN hardware

## Included Utilities

### `canlogger.py` - Universal CAN Logger Proxy

A cross-platform CAN logger proxy supporting multiple CAN interfaces through the python-can library:

**Supported Hardware:**
- **gs_usb** (CANable, candleLight, USB2CAN, BudgetCAN) - recommended for macOS
- **socketcan** (Linux kernel CAN) - recommended for Linux  
- **slcan** (LAWICEL/SLCAN serial adapters)
- **All python-can interfaces** (pcan, vector, ixxat, kvaser, etc.) - currently untested but should work via `--interface`

**Platform Support:**
- **macOS**: gs_usb (direct USB) or slcan
- **Linux**: socketcan (kernel driver) or gs_usb  
- **Windows**: Any python-can supported interface (currently untested)

### `rusoku_canlogger.py` - Rusoku TouCAN Proxy

A specialized proxy for Rusoku TouCAN USB adapters on macOS using the native MacCAN-TouCAN library:

**Supported Hardware:**
- Rusoku TouCAN USB (Model F4FS1)

**Platform Support:**
- **macOS** with MacCAN-TouCAN library

### `mock_uds_canlogger.py` - UDS Simulator

A mock CAN logger that generates synthetic UDS (Unified Diagnostic Services) over CAN traffic simulating ECU reprogramming sessions. Useful for testing diagnostic applications without real CAN hardware.

**Simulated Operations:**
- Complete ECU reprogramming sequence (identification, security access, memory erase, data transfer, reset)
- Realistic DTC (Diagnostic Trouble Code) reading and clearing
- OBD-II polling traffic between sessions
- Negative responses with automatic retries (busy, invalid key, transfer errors)

**Usage:**
```bash
# Basic usage
python3 utils/mock_uds_canlogger.py

# With verbose output and faster timing
python3 utils/mock_uds_canlogger.py -v --speed 2.0

# Single session (no loop)
python3 utils/mock_uds_canlogger.py --no-loop
```

## Quick Start

### 1. Install Dependencies

Install as a package (local checkout):
```bash
python3 -m pip install -e .
```

Optional extras:
```bash
python3 -m pip install -e .[gs_usb,serial]
```

**For canlogger.py:**
```bash
pip3 install python-can zeroconf

# For gs_usb support (CANable, etc.)
pip3 install gs_usb

# For serial/SLCAN support
pip3 install pyserial
```

**For rusoku_canlogger.py:**
```bash
# Install MacCAN-TouCAN library (macOS only)
# Download from: https://github.com/mac-can/RusokuCAN.dylib
cd RusokuCAN.dylib
./build_no.sh
make all
sudo make install

# Zeroconf service discovery
pip3 install zeroconf
```

### 2. Set Up Your CAN Interface

**Linux (SocketCAN):**
```bash
# Bring up the CAN interface
sudo ip link set can0 up type can bitrate 500000

# Run the logger proxy
python3 utils/canlogger.py --interface socketcan --channel can0
# or when installed as a package:
cancorder-canlogger --interface socketcan --channel can0
```

**macOS (gs_usb/CANable):**
```bash
# Requires sudo for USB access
sudo python3 utils/canlogger.py --interface gs_usb --channel 0
# or:
sudo cancorder-canlogger --interface gs_usb --channel 0
```

**macOS (Rusoku TouCAN):**
```bash
# No sudo required with TouCAN
python3 utils/rusoku_canlogger.py --channel 0
# or:
cancorder-rusoku-canlogger --channel 0
```

### 3. Connect CANcorder

1. **Start the logger proxy** using one of the commands above
2. **Open CANcorder** and go to connection settings
3. **Set connection parameters:**
   - **Protocol**: ECUconnect Logger
   - **Host**: `localhost` (or the machine's IP if remote)
   - **Port**: Auto-chosen value shown in the proxy log (≥42420 by default) or whatever you set via `--port`
4. **Click Connect** - CANcorder will now receive real-time CAN frames

## Zeroconf Service Discovery

Both `canlogger.py` and `rusoku_canlogger.py` advertise their TCP service via Zeroconf/mDNS (service type `_ecuconnect-log._tcp.local.`) when the [`zeroconf`](https://pypi.org/project/zeroconf/) package is installed.

- Default instance name: `ECUconnect-Logger <hostname>:<port>` (contains the required `ECUconnect-Logger` prefix).
- Override with `--service-name "ECUconnect-Logger My TouCAN"` or disable with `--no-zeroconf`.
- By default a free TCP port ≥42420 is selected automatically and published via Zeroconf; override with `--port` if a fixed port is required.
- TXT records expose metadata for richer client pickers:
  - `system`: hostname running the proxy
  - `process`: `canlogger.py` or `rusoku_canlogger.py`
  - `interface`: python-can interface (universal proxy only)
  - `channel`: interface channel or TouCAN index
  - `bitrate`: bus speed configuration (bits/second)
  - `port`: TCP server port

Discover services by running `dns-sd -B _ecuconnect-log._tcp` or any Zeroconf browser on your LAN.

## Wire Protocol Specification

The logger proxies implement the ECUconnect Logger TCP binary protocol. Each CAN frame is transmitted as a binary packet:

```
┌─────────────┬─────────────┬─────────┬─────────┬──────────────────┐
│ timestamp   │ can_id      │ ext     │ dlc     │ data             │
│ 8 bytes     │ 4 bytes     │ 1 byte  │ 1 byte  │ 0-64 bytes       │
└─────────────┴─────────────┴─────────┴─────────┴──────────────────┘
```

### Field Specifications

| Field | Offset | Size | Type | Endian | Description |
|-------|--------|------|------|--------|-------------|
| `timestamp` | 0 | 8 | `uint64_t` | Big | Microseconds since Unix epoch |
| `can_id` | 8 | 4 | `uint32_t` | Big | CAN arbitration ID |
| `ext` | 12 | 1 | `uint8_t` | N/A | 0=standard 11-bit ID, 1=extended 29-bit ID |
| `dlc` | 13 | 1 | `uint8_t` | N/A | Data length (0-8 for CAN, 0-64 for CAN-FD) |
| `data` | 14 | 0-64 | `uint8_t[]` | N/A | Raw CAN payload bytes |

**Total packet size**: 14 + dlc bytes (14-22 bytes for classic CAN)

### Implementation Examples

**Python (packing a frame):**
```python
import struct
import time

def pack_frame(can_id, is_extended, data):
    timestamp = int(time.time() * 1_000_000)  # microseconds
    ext_flag = 1 if is_extended else 0
    dlc = len(data)
    return struct.pack(">QIBB", timestamp, can_id, ext_flag, dlc) + bytes(data)

# Example: CAN ID 0x123, standard frame, data [0x01, 0x02]
packet = pack_frame(0x123, False, [0x01, 0x02])
```

**C (unpacking a frame):**
```c
#pragma pack(push, 1)
struct can_frame_header {
    uint64_t timestamp;  // big-endian
    uint32_t can_id;     // big-endian  
    uint8_t ext;
    uint8_t dlc;
};
#pragma pack(pop)

void parse_frame(const uint8_t* packet) {
    struct can_frame_header* hdr = (struct can_frame_header*)packet;
    uint64_t timestamp = be64toh(hdr->timestamp);
    uint32_t can_id = be32toh(hdr->can_id);
    uint8_t* data = (uint8_t*)(packet + 14);
    
    printf("CAN ID: 0x%X, DLC: %d, Timestamp: %lu\n", 
           can_id, hdr->dlc, timestamp);
}
```

### Testing the Protocol

**Using netcat to view raw packets:**
```bash
nc localhost <PORT_FROM_LOG> | xxd
```

**Expected output format:**
```
00000000: 0001 8f4c 45a6 8f20 0000 0123 0008 01ff  ...LE.. ...#....
00000010: 2345 6789 abcd ef00 0001 8f4c 45a6 8f25  #Eg........LE..%
          ^timestamp---^   ^id- ^e^d ^data--------
```

## Usage Examples

### Basic Usage

**Start logger with verbose output:**
```bash
python3 utils/canlogger.py -v
```

**Custom port and bitrate:**
```bash
python3 utils/canlogger.py --port 3000 --bitrate 250000
```

**Auto-detect CAN interface:**
```bash
sudo python3 utils/canlogger.py --auto
```

### Linux SocketCAN

**Set up virtual CAN for testing:**
```bash
sudo modprobe vcan
sudo ip link add vcan0 type vcan
sudo ip link set vcan0 up
python3 utils/canlogger.py --interface socketcan --channel vcan0
```

**Real CAN interface:**
```bash
sudo ip link set can0 up type can bitrate 500000
python3 utils/canlogger.py --interface socketcan --channel can0
```

### Multiple CAN Channels

Run multiple instances for different CAN buses:
```bash
# Terminal 1: CAN0 on port 42420
python3 utils/canlogger.py -i socketcan -c can0 -p 42420 &

# Terminal 2: CAN1 on port 42421  
python3 utils/canlogger.py -i socketcan -c can1 -p 42421 &
```

Configure multiple connections in CANcorder to capture from both buses.

### Rusoku TouCAN Specific

**Different bitrates:**
```bash
# 1000 kbit/s
python3 utils/rusoku_canlogger.py --bitrate 1000000

# 250 kbit/s
python3 utils/rusoku_canlogger.py --bitrate 250000

# 125 kbit/s
python3 utils/rusoku_canlogger.py --bitrate 125000
```

**Multiple TouCAN devices:**
```bash
python3 utils/rusoku_canlogger.py --channel 0 --port 42422 &
python3 utils/rusoku_canlogger.py --channel 1 --port 42423 &
```

### Mock UDS Simulator

**Test diagnostic applications without hardware:**
```bash
# Generate UDS reprogramming traffic
python3 utils/mock_uds_canlogger.py -v

# Speed up simulation (2x faster)
python3 utils/mock_uds_canlogger.py --speed 2.0

# Custom ECU IDs
python3 utils/mock_uds_canlogger.py --tester-id 0x7E2 --ecu-id 0x7EA
```

The simulator generates:
- **UDS Services**: DiagnosticSessionControl (0x10), SecurityAccess (0x27), ReadDTCInformation (0x19), TransferData (0x36), ECUReset (0x11), and more
- **OBD-II Polling**: Service 01 (live data), Service 03 (stored DTCs), Service 07 (pending DTCs)
- **Error Conditions**: Busy responses (0x21), invalid keys (0x35), transfer errors (0x71-0x73) with automatic retries

## Implementing Custom Logger Backends

The wire protocol specification above enables you to create custom logger backends for any CAN hardware. A minimal implementation needs to:

1. **Connect to your CAN hardware** using its native API
2. **Open a TCP server** on a chosen port (defaults to an auto-picked value ≥42420)
3. **For each received CAN frame**:
   - Pack it into the 14+dlc byte binary format
   - Broadcast to all connected TCP clients
4. **Handle client connections/disconnections** gracefully

The provided utilities serve as reference implementations showing best practices for client management, error handling, and cross-platform compatibility.

## Troubleshooting

### Connection Issues

**"No CAN device found":**
- Check USB connection: `lsusb` (Linux) or `system_profiler SPUSBDataType | grep -i can` (macOS)
- Try auto-detection: `--auto` flag
- Verify correct interface/channel parameters

**"Access denied" on macOS:**
- Use `sudo` for gs_usb: `sudo python3 utils/canlogger.py`
- TouCAN doesn't require sudo if library is properly installed

**"Network interface not found" on Linux:**
- Bring up interface: `sudo ip link set can0 up type can bitrate 500000`
- Check interface exists: `ip link show can0`

### Performance Issues

**High CPU usage:**
- Normal with high-volume CAN traffic
- Disable verbose output (`-v` flag) to reduce overhead
- Consider using SocketCAN on Linux for better kernel-level performance

**Clients not receiving data:**
- Verify CAN bus has traffic (use `-v` to see frames)
- Check firewall allows the chosen TCP port (see proxy log/Zeroconf entry)
- Ensure CAN bitrate matches the bus
- Test with: `nc localhost <PORT_FROM_LOG> | xxd`

### Library Issues

**RusokuCAN library not found:**
- Install from: https://github.com/mac-can/RusokuCAN.dylib
- Ensure `libUVCANTOU.dylib` is in `/usr/local/lib`
- Use `--library` flag to specify custom path

**Python dependencies missing:**
- Install python-can: `pip3 install python-can`
- For gs_usb: `pip3 install gs_usb`
- For serial: `pip3 install pyserial`

## Advanced Configuration

### Custom Bitrates (canlogger.py)

```bash
# Common automotive bitrates
python3 utils/canlogger.py --bitrate 125000   # Low-speed CAN
python3 utils/canlogger.py --bitrate 250000   # Medium-speed CAN  
python3 utils/canlogger.py --bitrate 500000   # High-speed CAN (default)
python3 utils/canlogger.py --bitrate 1000000  # CAN-FD nominal bitrate
```

### Serial/SLCAN Configuration

```bash
# USB-serial CAN adapter
python3 utils/canlogger.py --interface slcan --channel /dev/ttyUSB0

# macOS USB-serial
python3 utils/canlogger.py --interface slcan --channel /dev/tty.usbmodem1234
```

### Production Deployment

For production environments, consider:
- Running as systemd service (Linux) or launchd (macOS)
- Using non-privileged user with appropriate group membership (dialout, can)
- Firewall configuration for remote connections
- Log rotation and monitoring
- Automatic restart on CAN bus errors

## Related Projects

- **[CANcorder](../CANcorder)** - The main CAN bus logger and analyzer application
- **[python-can](https://python-can.readthedocs.io/)** - Python CAN library used by canlogger.py
- **[MacCAN-TouCAN](https://github.com/mac-can/RusokuCAN.dylib)** - Native macOS library for Rusoku adapters
- **[SocketCAN](https://www.kernel.org/doc/html/latest/networking/can.html)** - Linux kernel CAN subsystem

## License

This project is part of the CANcorder ecosystem. See the main CANcorder project for licensing information.
