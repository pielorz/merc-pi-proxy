import sys
from evdev import InputDevice, ecodes

path = '/dev/input/event0'

try:
	merc = InputDevice(path)
	print(f"Spying on: {merc.name} Path: {path}")
	print("Press keys on your Merc keyboard to see their codes...")
except Exception as e:
	print(f"Error opening port: {e}")
	sys.exit(1)

for event in merc.read_loop():
	if event.type == ecodes.EV_KEY:
		# Prints when pressed (1) or released (0)
		print(f"Key Code: {event.code} | Key Name: {ecodes.KEY[event.code]} | Value: {event.value}")
