#!/bin/bash
# run_tests_8h.sh
# Runs the full bazaar-bot test suite continuously for 8 hours (28800 seconds)
# Logs output and any failures to test_run.log

END_TIME=$((SECONDS+28800))
export BAZAAR_KEY="test_dummy_key"

echo "Starting 8-hour exhaustive test run..."
echo "End time: $(date -d @$END_TIME)"

while [ $SECONDS -lt $END_TIME ]; do
    echo "--- Test Run at $(date) ---" | tee -a test_run.log
    
    # Run tests using pytest or unittest
    python3 -m unittest discover -s tests -p "test_*.py" >> test_run.log 2>&1
    
    if [ $? -ne 0 ]; then
        echo "🚨 ALERT: Tests failed at $(date). Check test_run.log" | tee -a test_run.log
        # Optionally, don't exit to keep testing, or exit to debug:
        # exit 1 
    else
        echo "✅ All tests passed. Waiting 5 minutes for next cycle..." | tee -a test_run.log
    fi
    
    # Sleep to simulate intermittent testing vs continuous hammering (adjust as needed)
    sleep 300
done

echo "🎉 8-hour test run completed successfully!"
