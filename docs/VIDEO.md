# Recording the demo video

1. Start the simulator with the scripted scenario, at night time:
   `python -m simulator.sim_server --speed 30 --scenario demo --night`
2. Start the agent on the simulator: `python -m agent run --config config/config.sim.yaml`
3. Open two browser windows side by side: the simulator control page (http://127.0.0.1:8090) and the agent dashboard (http://127.0.0.1:8080). Optionally a third window with `data/outbox/sms.log` open in a text editor that reloads, to show the SMS.
4. Record the screen (OBS Studio, or Win+G on Windows) for about 8 minutes.

What happens (simulated time, 30x faster than real time):

| Real time | Simulated | Event | What to show on the dashboard |
|---|---|---|---|
| 0:00 | 02:50 | Start, the agent learns normal values | Wells marked "apprentissage", then "Normal" |
| 1:10 | 03:25 | Pressure drop on PUITS-01 | Emergency, diagnosis, SMS to the on-call technician |
| 2:04 | 03:52 | Radio repeater R2 fails | One alarm and one SMS for 4 silent wells |
| 2:56 | 04:18 | Gas lift network failure on MAN-A | Network alarm, wells' consequences grouped, escalation |
| 4:24 | 05:02 | Annulus pressure rises on PUITS-11 | Well integrity alarm, % of MAASP |
| 5:14 | 05:27 | Fire on PUITS-07 | The 3 contacts paged at once |
| 5:44 | 05:42 | Flowline restriction on MAN-C | One manifold alarm instead of 4 well alarms |
| 6:36 | 06:08 | I/O card fault on PUITS-09 | One alarm, 3 faulty transmitters grouped under it |
| 7:14 | 06:27 | Choke plugging on PUITS-07 | "Choke probably plugging": wellhead up, line pressure down |
| 8:20 | 07:00 | Daily report | Report e-mail in `data/outbox` |

Click "Acquitter" on one alarm during the recording to show that escalation stops.

Say clearly in the video that the field is simulated, and that the agent reads it over OPC UA exactly as it would read a real SCADA.
