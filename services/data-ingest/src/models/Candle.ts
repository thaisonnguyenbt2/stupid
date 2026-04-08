import mongoose from 'mongoose';

const candleSchema = new mongoose.Schema({
  symbol:    { type: String, required: true },
  interval:  { type: String, required: true },
  timestamp: { type: Date, required: true },
  open:      { type: Number, required: true },
  high:      { type: Number, required: true },
  low:       { type: Number, required: true },
  close:     { type: Number, required: true },
  volume:    { type: Number, default: 0 },
  tickVolume:{ type: Number, default: 0 },   // Tick count per candle (reliable proxy)
  isFinal:   { type: Boolean, default: false }
});

candleSchema.index({ symbol: 1, interval: 1, timestamp: 1 }, { unique: true });
candleSchema.index({ timestamp: 1 }, { expireAfterSeconds: 604800 }); // 7-day TTL

export const Candle = mongoose.model('Candle', candleSchema);
