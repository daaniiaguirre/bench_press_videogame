# Run with your Arduino connected:

python encoder_visualizer.py                    # auto-detects Arduino port
python encoder_visualizer.py --port COM3        # Windows
python encoder_visualizer.py --port /dev/ttyACM0   # Linux
python encoder_visualizer.py --port /dev/cu.usbmodem1401  # macOS


# Test without hardware:
python encoder_visualizer.py --demo

# Final videogame:
python final_videogame.py