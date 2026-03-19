# Smart Heatpump Controller

An intelligent thermostat controller for Home Assistant that optimises heat pump operation based on real-time COP, solar surplus, and weather forecast.

- **Solar boost:** Automatically raises the setpoint when you are exporting solar electricity to the grid, storing free energy as heat in your floor.
- **COP-aware pre-heating:** Pre-heats your home hours in advance of cold periods while outdoor temperatures — and therefore heat pump efficiency — are still favourable.
- **Conservation mode:** Reduces heating to the minimum when COP is poor, and waits for the next efficient window before heating again.
- **Fully local:** All logic runs in AppDaemon. No cloud dependency, no API keys.
- **Easy configuration:** All parameters are adjustable from the Home Assistant dashboard via sliders — no YAML editing required after installation.

Requires AppDaemon, a `climate` entity for your thermostat, and a net power sensor (W) from a smart meter or P1 dongle.
