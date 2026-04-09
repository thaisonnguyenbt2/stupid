#!/bin/bash
# Oracle Cloud Free-Tier Capacity Auto-Retrier
# Loops the provisioning request until A1 hardware becomes available in your region.

echo "=========================================================="
echo "    Oracle Cloud 'Out of Host Capacity' Auto-Retrier"
echo "=========================================================="
echo "Oracle frequently runs out of A1.Flex compute space in"
echo "popular regions like AP-SINGAPORE-1. This script will"
echo "continuously attempt to provision your server."
echo "Press [CTRL+C] at any time to stop."
echo "=========================================================="

while true; do
  echo "Attempting to provision at $(date)..."
  
  # Run the native make command. If it succeeds, the exit code is 0 and we break.
  make ocl-provision
  res=$?
  
  if [ $res -eq 0 ]; then
    echo "🎉 SUCCESS: Oracle capacity allocated and instance is booting!"
    exit 0
  fi
  
  echo "❌ Capacity error. Retrying in 60 seconds..."
  sleep 60
done
