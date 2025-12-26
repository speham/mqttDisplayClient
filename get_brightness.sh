#!/bin/bash
/usr/bin/ddcutil --display 1 getvcp 10 | /usr/bin/awk '{print $NF}'
