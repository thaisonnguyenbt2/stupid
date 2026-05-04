const { MongoClient } = require('mongodb');

async function run() {
  const client = new MongoClient('mongodb://localhost:27017');
  await client.connect();
  const db = client.db('trading');
  
  const now = new Date();
  const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  
  // Get trades
  const trades = await db.collection('trades').find({
    openedAt: { $gte: startOfDay }
  }).toArray();
  
  console.log(`Found ${trades.length} trades today.`);
  
  let wins = 0;
  let losses = 0;
  
  trades.forEach(t => {
      console.log(`[${t.slot}] ${t.strategy} ${t.direction} | R:R ${t.meta.tp_mult}:${t.meta.sl_mult} | Profit: ${t.realizedPnl} | Meta: ${t.meta.rule}`);
      if (t.realizedPnl > 0) wins++;
      if (t.realizedPnl < 0) losses++;
  });
  console.log(`Today's performance: ${wins} wins, ${losses} losses`);
  
  await client.close();
}

run().catch(console.error);
