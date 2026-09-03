#!/bin/sh
set -eu

plist="$HOME/Library/LaunchAgents/dev.jomcgi.session-collector.plist"
launchctl bootout "gui/$(id -u)" "$plist"
