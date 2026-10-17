"""Read-only Windows IPv4 interface validation; no DNS or network changes."""

import ctypes
import socket
import struct
import sys
from ctypes import wintypes
from ipaddress import IPv4Address, IPv4Interface


class _AddressRow(ctypes.Structure):
    address: int
    mask: int
    _fields_ = [
        ("address", wintypes.DWORD),
        ("index", wintypes.DWORD),
        ("mask", wintypes.DWORD),
        ("broadcast", wintypes.DWORD),
        ("reassembly", wintypes.DWORD),
        ("unused", wintypes.WORD),
        ("flags", wintypes.WORD),
    ]


def local_interfaces() -> tuple[IPv4Interface, ...]:
    """GetIpAddrTable exposes assigned IPv4 addresses and actual subnet masks."""
    if sys.platform != "win32":
        raise OSError("Explicit LAN host validation requires Windows")
    get_table = ctypes.WinDLL("iphlpapi").GetIpAddrTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
    ]
    get_table.restype = wintypes.DWORD
    size = wintypes.ULONG()
    status = get_table(None, ctypes.byref(size), False)
    if status != 122:  # ERROR_INSUFFICIENT_BUFFER: query the required allocation.
        raise ctypes.WinError(status)
    buffer = ctypes.create_string_buffer(size.value)
    status = get_table(buffer, ctypes.byref(size), False)
    if status:
        raise ctypes.WinError(status)
    count = wintypes.DWORD.from_buffer(buffer).value
    alignment = ctypes.alignment(_AddressRow)
    offset = (ctypes.sizeof(wintypes.DWORD) + alignment - 1) // alignment * alignment
    result: list[IPv4Interface] = []
    for index in range(count):
        row = _AddressRow.from_buffer(
            buffer, offset + index * ctypes.sizeof(_AddressRow)
        )
        # Win32 DWORDs contain network-order bytes in native-endian integer storage.
        address = socket.inet_ntoa(struct.pack("=I", row.address))
        mask = socket.inet_ntoa(struct.pack("=I", row.mask))
        result.append(IPv4Interface(f"{address}/{mask}"))
    return tuple(result)


def local_ipv4_addresses() -> tuple[str, ...]:
    """Assigned selectable unicast addresses, without probing the network."""
    addresses = {
        interface.ip
        for interface in local_interfaces()
        if not (
            interface.ip.is_multicast
            or interface.ip.is_reserved
            or interface.ip.is_loopback
            or int(interface.ip) >> 24 == 0
        )
        and (
            interface.network.prefixlen >= 31
            or interface.ip
            not in (
                interface.network.network_address,
                interface.network.broadcast_address,
            )
        )
    }
    return ("127.0.0.1", *(str(address) for address in sorted(addresses)))


def validate_host(host: str) -> str:
    address = IPv4Address(host)  # Strict numeric IPv4, never hostname resolution.
    if host == "127.0.0.1":
        return host
    if (
        address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or int(address) >> 24 == 0
    ):
        raise ValueError("Web host must be a specific local unicast IPv4 address")
    assigned = tuple(
        interface for interface in local_interfaces() if interface.ip == address
    )
    if not assigned:
        raise ValueError(f"Web host {host} is not assigned to a local interface")
    if all(
        interface.network.prefixlen < 31
        and address
        in (interface.network.network_address, interface.network.broadcast_address)
        for interface in assigned
    ):
        raise ValueError("Web host cannot be a subnet or broadcast address")
    return str(address)
