#!/bin/bash

chmod +x start.sh stop.sh status.sh
chmod +x run_foreground.sh
chmod +x npu_monitor.py query_stats.py test_tool.py
chmod +x test_parse.py
chmod +x test_lifecycle.sh tests/fake_npu_smi.sh
chmod +x healthcheck.py
chmod +x deploy/*.sh

echo "Setup completed!"
echo ""
echo "Quick start:"
echo "  1. Start monitor:    ./start.sh"
echo "  2. Check status:     ./status.sh"
echo "  3. Query stats:      python3 query_stats.py --today"
echo "  4. Stop monitor:     ./stop.sh"
echo ""
echo "Cluster deployment: see 使用指导.md and deploy/*.sh"
