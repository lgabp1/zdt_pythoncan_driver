"""
Defines the low level inteface, taking input commands and sending or retrieving corresponding data.
"""

import time
from typing import Optional, Iterable
from can import Bus, BusABC, Message
from logging import Logger

from .lib_threading import Threader

class CANInterface:
    """CAN Interface implementation."""
    buses: dict[tuple[str, str], BusABC | None] = {} # { (interface, channel): bus}
    message_fifos: dict[ tuple[str, str, int], list[Message]] = {}  # All messages received will be stored here. { (interface, channel, can_id): bus}

    def __init__(self, interface: str, channel: str, bitrate: int, max_queue_size: int = 10, logger: Optional[Logger] = None):
        """CAN Interface implementation.
        
        Args:
            interface (str): interface to use
            channel (str): channel to use
            bitrate (int): bitrate to use
            max_queue_size (int, optional): Maximum FIFO queue size for a CAN id 
            logger (Logger, optional): optional logger
        
        Note: uses static buses to allow multiple device instances"""
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate
        self.logger = logger
        self.max_queue_size = max_queue_size

        self._threader = Threader(do_finally=self.close)

        if self.logger:
            with self._threader.get_lock():
                self.logger.debug("CANInterface instance created.")

    def _loop(self):
        while self._threader._running:
            bus = CANInterface.buses[(self.interface, self.channel)]
            if bus:
                msg = bus.recv(1.0)
                if msg:
                    with self._threader.get_lock():
                        if self.logger:
                            self.logger.debug(f"Message received: {msg}")
                    can_id = msg.arbitration_id
                    with self._threader.get_lock():
                        if (self.interface, self.channel, can_id) in self.message_fifos: # Append to fifo
                            self.message_fifos[(self.interface, self.channel, can_id)].append(msg)
                            if len(self.message_fifos[(self.interface, self.channel, can_id)]) > self.max_queue_size:
                                self.message_fifos[(self.interface, self.channel, can_id)].pop(0) # remove oldest message 
                        else:
                            self.message_fifos[(self.interface, self.channel, can_id)] = [msg] # New fifo

    def clear_fifo(self, ids: Optional[Iterable[int]] = []) -> None:
        """Clear FIFOs. If an iterable of can ids is given, will clear the corresponding fifo only"""
        # temp
        if not ids:
            with self._threader.get_lock():
                self.message_fifos.clear()
        else:
            for can_id in ids:
                with self._threader.get_lock():
                    if can_id in self.message_fifos:
                        self.message_fifos[(self.interface, self.channel, can_id)].clear()

    def open(self) -> None:
        if (self.interface, self.channel) not in CANInterface.buses or CANInterface.buses[(self.interface, self.channel)] is None:
            CANInterface.buses[(self.interface, self.channel)] = Bus(self.channel, self.interface, bitrate=self.bitrate, data_bitrate=self.bitrate, fd=False, receive_own_messages=False)
            self._threader.start_threaded(self._loop)

    def close(self) -> None:
        self._threader.stop()
        bus = CANInterface.buses[(self.interface, self.channel)]
        if bus:
            bus.shutdown()
            bus = None # Reset
    
    def receive_from(self, can_id: int, timeout: float | None, check_frequency: Optional[float] = 100) -> Message | None:
        """Receive a message from given can_id. If timeout is None, will wait indefinitely."""
        if not check_frequency or check_frequency <= 0:
            raise ValueError(f"check_frequency should be non negative, not {check_frequency} !")

        if timeout is None:
            while True: # Wait indefinitely
                with self._threader.get_lock():
                    if (self.interface, self.channel, can_id) in self.message_fifos:
                        if len(self.message_fifos[(self.interface, self.channel, can_id)]) > 0:
                            return self.message_fifos[(self.interface, self.channel, can_id)].pop(0)
                time.sleep(1 / check_frequency)
        else:
            start_time = time.time()
            while time.time() - start_time < timeout: # Wait for at most timeout seconds
                with self._threader.get_lock():
                    if (self.interface, self.channel, can_id) in self.message_fifos:
                        print(len(self.message_fifos[(self.interface, self.channel, can_id)]))
                        if len(self.message_fifos[(self.interface, self.channel, can_id)]) > 0:
                            return self.message_fifos[(self.interface, self.channel, can_id)].pop(0)
                time.sleep(1 / check_frequency)
        return None
    
    def send(self, msg: Message, timeout: Optional[float] = None) -> bool:
        """Sends a message. Returns False if timeout, True if successful."""
        if (self.interface, self.channel) not in CANInterface.buses:
            raise KeyError(f"BusABC object is missing for {(self.interface, self.channel)}")

        bus = CANInterface.buses[(self.interface, self.channel)]
        if bus is None:
            raise ValueError(f"Bus {bus} was not opened!")
        
        try:
            bus.send(msg, timeout=timeout)
            if self.logger:
                with self._threader.get_lock():
                    self.logger.debug(f"Message sent: {msg}")
            return True
        except Exception:
            if self.logger:
                with self._threader.get_lock():
                    self.logger.info(f"Message sent timeout ({timeout=}) for: {msg}")
            return False
        
class ZDTCANInterface(CANInterface):
    """CAN interface made for communication with the ZDT controller."""

    def send_cmd(self, can_id: int, payload: bytearray, timeout: Optional[float] = None) -> bool:
        """Sends a command. Returns False if timeout, True if successful."""
        msg = Message(arbitration_id=can_id, data=payload, is_extended_id=True, is_fd=False)
        return self.send(msg, timeout=timeout)
    
    def receive_cmd_from(self, can_id: int, timeout: Optional[float] = None, check_frequency: Optional[float] = 100) -> tuple[int, bytearray] | tuple[None, None]:
        """Receive command payload. Returns None if failed to retrieve."""
        msg = self.receive_from(can_id, timeout, check_frequency)
        if msg is None:
            return (None, None)
        else:
            return (msg.arbitration_id, msg.data)
    
    def clear_queues_of(self, mot_id: int) -> None:
        """Clear queues related to given mot_id"""
        ids = [(mot_id<<8) + offset for offset in range(0,5)]
        self.clear_fifo(ids)

    def clear_queues_all(self) -> None:
        """Clear all queues"""
        self.clear_fifo()