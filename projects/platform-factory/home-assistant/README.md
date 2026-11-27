# Home Assistant

Home Assistant runs at home.jomcgi.dev for the Authentik Admins group.
Zigbee2MQTT opens the SONOFF Dongle Max MG24 on node-2 and talks to Home
Assistant through Mosquitto. Joe chose Zigbee2MQTT over ZHA
([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-913#comment-12891)).

```mermaid
flowchart LR
  E[edge Envoy] -->|8123| HA[home-assistant]
  HA -->|1883| M[mosquitto]
  Z[zigbee2mqtt, node-2] -->|1883| M
  Z -->|/dev/zigbee| D[(MG24 dongle)]
```

## First setup

1. Open home.jomcgi.dev and create the owner account.
2. Add the MQTT integration with broker `mosquitto` and port `1883`.

## Pair a device

1. In Home Assistant, turn on the Zigbee2MQTT Bridge **Permit join** switch.
2. Power the device near node-2. It appears in Home Assistant when it joins.
3. Rename the device to its room.

The Zigbee2MQTT web UI has no route. Reach it with
`kubectl -n home-assistant port-forward deploy/zigbee2mqtt 8080`.

## HTTP settings

Home Assistant keeps its HTTP settings in `/config/.storage/http`. The
`http-settings` init container writes `resources/http-storage.json` there on
every start, so the edge proxy stays trusted. Change those settings in git;
a change in Settings → System → Network is lost at the next restart.

## Moving the dongle

The zigbee2mqtt-data volume holds the network key and paired devices on
node-2. If the dongle moves to another node, move the volume data and change
the nodeSelector, or pair every device again.
