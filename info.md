# Smart Heatpump Controller

An intelligent thermostat controller for Home Assistant that optimises heat pump operation based on real-time COP, solar surplus, and weather forecast.

- **Solar boost:** Stores free solar energy as heat when you're exporting to the grid.
- **COP-aware pre-heating:** Pre-heats before cold periods while the heat pump is still efficient.
- **Conservation mode:** Reduces heating when COP is poor and waits for the next efficient window.
- **Fully local:** No cloud, no API keys, no AppDaemon required.
- **Easy setup:** Install via HACS, add the integration, pick your entities from a dropdown — done.

All parameters are adjustable from the dashboard via sliders. No YAML editing required.
