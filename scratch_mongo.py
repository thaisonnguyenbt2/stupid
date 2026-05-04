import pymongo
from datetime import datetime, timedelta
import pandas as pd

client = pymongo.MongoClient('mongodb://localhost:27017/trading')
db = client.get_database()
ticks = db['ticks']

# Get the most recent tick to define "today"
last_tick = ticks.find_one(sort=[('time', pymongo.DESCENDING)])
if not last_tick:
    print("No ticks in MongoDB!")
    exit()

end_time = last_tick['time']
start_time = end_time - timedelta(days=1)

print(f"Data range: {start_time} to {end_time}")
count = ticks.count_documents({'time': {'$gte': start_time}})
print(f"Tick count: {count}")
