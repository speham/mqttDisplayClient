#!/bin/bash
/usr/bin/ddcutil --display 1 setvcp 10 97 | /usr/bin/awk '{print $NF}'
