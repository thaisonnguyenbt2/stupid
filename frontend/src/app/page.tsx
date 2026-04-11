'use client';
import React, { useEffect, useState, useRef, useCallback } from 'react';

type Trade = {
  _id: string;
  symbol: string;
  direction: string;
  status: string;
  entryPrice: number;
  tp?: number;
  sl?: number;
  exitPrice?: number;
  entryTime: number;
  exitTime?: number;
  pnl?: number;
  peakProfit?: number;
  peakLoss?: number;
  signalType?: string;
  closeReason?: string;
  meta?: Record<string, any>;
  lotSize?: number;
};

type Indicators = {
  m1_rsi?: number;
  m1_ema21?: number;
  m1_upper_bb?: number;
  m1_lower_bb?: number;
  m5_ema9?: number;
  m5_ema21?: number;
  m5_ema50?: number;
  m5_rsi?: number;
  m5_atr?: number;
};

const API_URL = process.env.NEXT_PUBLIC_ANALYZER_PORT
  ? `http://localhost:${process.env.NEXT_PUBLIC_ANALYZER_PORT}`
  : 'http://localhost:4002';

const WS_NOTIFICATION = process.env.NEXT_PUBLIC_NOTIFICATION_WS || 'ws://localhost:4003/ws';
const WS_TICK = 'ws://localhost:4000/ws';

export default function Dashboard() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [livePrice, setLivePrice] = useState<number | null>(null);
  const [indicators, setIndicators] = useState<Indicators>({});
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [notifications, setNotifications] = useState<any[]>([]);
  const wsTickRef = useRef<WebSocket | null>(null);
  const wsNotifRef = useRef<WebSocket | null>(null);

  const fetchTrades = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/paper-trades`);
      if (res.ok) {
        const data = await res.json();
        setTrades(data.trades || []);
        if (data.livePrice) setLivePrice(data.livePrice);
        if (data.indicators) setIndicators(data.indicators);
      }
    } catch (err) {
      console.error('Fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch on mount
  useEffect(() => {
    fetchTrades();
  }, [fetchTrades]);

  // WebSocket: Live ticks
  useEffect(() => {
    const connect = () => {
      try {
        const ws = new WebSocket(WS_TICK);
        wsTickRef.current = ws;
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'trade' && msg.data) {
              const latest = msg.data.reduce(
                (best: any, t: any) => (!best || t.t > best.t ? t : best), null
              );
              if (latest) setLivePrice(latest.p);
            }
          } catch {}
        };
        ws.onclose = () => setTimeout(connect, 5000);
        ws.onerror = () => ws.close();
      } catch {}
    };
    connect();
    return () => { wsTickRef.current?.close(); };
  }, []);

  // WebSocket: Trade notifications
  useEffect(() => {
    const connect = () => {
      try {
        const ws = new WebSocket(WS_NOTIFICATION);
        wsNotifRef.current = ws;
        ws.onmessage = (ev) => {
          try {
            const event = JSON.parse(ev.data);
            if (event.type === 'TRADES_UPDATE') {
              if (event.trades) setTrades(event.trades);
              if (event.livePrice) setLivePrice(event.livePrice);
              if (event.indicators) setIndicators(event.indicators);
            } else {
              setNotifications(prev => [event, ...prev].slice(0, 5));
              // Play alert sound for actual notifications
              try {
                const audio = new Audio('https://actions.google.com/sounds/v1/ui/message_notification.ogg');
                audio.volume = 0.6;
                audio.play().catch(() => {});
              } catch {}
              // We don't need to fetchTrades() here anymore because TRADES_UPDATE comes 
              // at the same time and handles the state update efficiently.
            }
          } catch {}
        };
        ws.onclose = () => setTimeout(connect, 5000);
        ws.onerror = () => ws.close();
      } catch {}
    };
    connect();
    return () => { wsNotifRef.current?.close(); };
  }, [fetchTrades]);

  const getUnrealized = (trade: Trade) => {
    if (!livePrice || trade.status !== 'OPEN') return null;
    return trade.direction === 'LONG'
      ? (livePrice - trade.entryPrice) * 1.0
      : (trade.entryPrice - livePrice) * 1.0;
  };

  const totalPnL = trades.reduce((sum, t) => {
    if (t.status === 'CLOSED') return sum + (t.pnl || 0);
    if (t.status === 'OPEN') return sum + (getUnrealized(t) || 0);
    return sum;
  }, 0);

  const closedTrades = trades.filter(t => t.status === 'CLOSED');
  const wins = closedTrades.filter(t => (t.pnl || 0) > 0).length;
  const losses = closedTrades.filter(t => (t.pnl || 0) < 0).length;
  const winRate = closedTrades.length > 0 ? ((wins / closedTrades.length) * 100).toFixed(1) : '0.0';
  const openCount = trades.filter(t => t.status === 'OPEN').length;
  const prepareCount = trades.filter(t => t.status === 'PREPARE').length;

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this trade?')) return;
    try {
      await fetch(`${API_URL}/api/paper-trades/${id}`, { method: 'DELETE' });
      fetchTrades();
    } catch (err) {
      console.error('Delete error:', err);
    }
  };

  const handleClearAll = async () => {
    if (!confirm('Delete ALL paper trades? This cannot be undone.')) return;
    setClearing(true);
    try {
      await fetch(`${API_URL}/api/paper-trades`, { method: 'DELETE' });
      fetchTrades();
    } catch (err) {
      console.error('Clear error:', err);
    } finally {
      setClearing(false);
    }
  };

  const formatDuration = (entry: number, exit?: number) => {
    const end = exit || Date.now();
    const diff = Math.floor((end - entry) / 1000);
    if (diff < 60) return `${diff}s`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
    return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
  };

  const strategyColor = (s: string) => {
    switch (s) {
      case 'EMA_PULLBACK': return '#60a5fa';
      case 'BB_REVERSION': return '#a78bfa';
      case 'INST_BREAKOUT': return '#fbbf24';
      default: return '#94a3b8';
    }
  };

  return (
    <div style={{ padding: '24px', maxWidth: 1500, margin: '0 auto' }}>

      {/* Header */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: 'var(--bg-card)', padding: '20px 28px', borderRadius: 'var(--radius-lg)',
        border: '1px solid var(--border-subtle)', marginBottom: 24,
        backdropFilter: 'blur(16px)'
      }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <h1 style={{
              fontSize: 26, fontWeight: 800,
              background: 'linear-gradient(90deg, #60a5fa, #34d399)',
              WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent'
            }}>
              XAU/USD Paper Trading
            </h1>
            {livePrice && (
              <div style={{
                background: 'var(--accent-green-bg)', padding: '6px 14px', borderRadius: 10,
                border: '1px solid var(--accent-green-border)', display: 'flex', alignItems: 'center', gap: 8
              }}>
                <div style={{ width: 8, height: 8, borderRadius: 4, background: 'var(--accent-green)' }} className="animate-pulse" />
                <span style={{ color: 'var(--accent-green)', fontWeight: 800, fontSize: 18, fontFamily: 'monospace' }}>
                  ${livePrice.toFixed(2)}
                </span>
              </div>
            )}
          </div>
          <p style={{ color: 'var(--text-muted)', marginTop: 4, fontSize: 13 }}>
            Live strategies: EMA Pullback · BB Reversion · Inst. Breakout | 0.01 lot (1 oz)
          </p>
        </div>

        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <button
            onClick={handleClearAll}
            disabled={clearing}
            style={{
              background: 'var(--accent-red-bg)', border: '1px solid var(--accent-red-border)',
              color: 'var(--accent-red)', padding: '8px 16px', borderRadius: 10, fontSize: 13, fontWeight: 600,
              cursor: clearing ? 'not-allowed' : 'pointer', opacity: clearing ? 0.5 : 1,
              transition: 'all 0.2s'
            }}
          >
            {clearing ? '...' : '🧹 Clear All'}
          </button>
          <StatCard label="Net P/L" value={`${totalPnL >= 0 ? '+' : ''}$${totalPnL.toFixed(2)}`} color={totalPnL >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'} />
          <StatCard label="Win Rate" value={`${winRate}%`} color="var(--accent-blue)" />
          <StatCard label="W / L" value={`${wins} / ${losses}`} color="var(--accent-purple)" />
          <StatCard label="Open" value={`${openCount}`} color="var(--accent-yellow)" />
          <StatCard label="Prepare" value={`${prepareCount}`} color="var(--accent-purple)" />
        </div>
      </div>

      {/* Notification Toast */}
      {notifications.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          {notifications.slice(0, 2).map((n, i) => (
            <div key={i} className="fade-in" style={{
              background: n.type === 'TRADE_OPEN' ? 'var(--accent-green-bg)' : n.type === 'TRADE_CLOSE' ? 'var(--accent-red-bg)' : 'var(--bg-card)',
              border: `1px solid ${n.type === 'TRADE_OPEN' ? 'var(--accent-green-border)' : n.type === 'TRADE_CLOSE' ? 'var(--accent-red-border)' : 'var(--border-subtle)'}`,
              padding: '10px 16px', borderRadius: 10, marginBottom: 8, fontSize: 12, color: 'var(--text-secondary)'
            }}>
              <b>{n.title}</b> — {new Date(n.timestamp).toLocaleTimeString()}
            </div>
          ))}
        </div>
      )}

      {/* Live Indicators Panel */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
        gap: 10, marginBottom: 20
      }}>
        <IndicatorCard label="M1 RSI" value={indicators.m1_rsi} warn={indicators.m1_rsi !== undefined && (indicators.m1_rsi > 75 || indicators.m1_rsi < 25)} />
        <IndicatorCard label="M1 EMA21" value={indicators.m1_ema21} />
        <IndicatorCard label="M1 Upper BB" value={indicators.m1_upper_bb} />
        <IndicatorCard label="M1 Lower BB" value={indicators.m1_lower_bb} />
        <IndicatorCard label="M5 EMA9" value={indicators.m5_ema9} />
        <IndicatorCard label="M5 EMA21" value={indicators.m5_ema21} />
        <IndicatorCard label="M5 EMA50" value={indicators.m5_ema50} />
        <IndicatorCard label="M5 RSI" value={indicators.m5_rsi} warn={indicators.m5_rsi !== undefined && (indicators.m5_rsi > 70 || indicators.m5_rsi < 30)} />
        <IndicatorCard label="M5 ATR" value={indicators.m5_atr} />
      </div>

      {/* Trend Analysis Summary */}
      {(() => {
        const { m5_ema9, m5_ema21, m5_ema50, m5_rsi, m5_atr, m1_rsi, m1_upper_bb, m1_lower_bb } = indicators;
        const price = livePrice;
        if (!price || !m5_ema9 || !m5_ema21 || !m5_ema50 || m5_rsi === undefined || m1_rsi === undefined) return null;

        // Determine trend direction from M5 EMA alignment
        const emaBull = m5_ema9 > m5_ema21 && m5_ema21 > m5_ema50;
        const emaBear = m5_ema9 < m5_ema21 && m5_ema21 < m5_ema50;
        const priceAboveEma = price > m5_ema9 && price > m5_ema21;
        const priceBelowEma = price < m5_ema9 && price < m5_ema21;

        // Confidence scoring
        let bullScore = 0;
        let bearScore = 0;

        if (emaBull) bullScore += 2;
        if (emaBear) bearScore += 2;
        if (priceAboveEma) bullScore += 1;
        if (priceBelowEma) bearScore += 1;
        if (m5_rsi > 55) bullScore += 1;
        if (m5_rsi < 45) bearScore += 1;
        if (m5_rsi > 65) bullScore += 1;
        if (m5_rsi < 35) bearScore += 1;
        if (m1_rsi > 60) bullScore += 1;
        if (m1_rsi < 40) bearScore += 1;
        if (m1_upper_bb && price > m1_upper_bb) bullScore += 1;
        if (m1_lower_bb && price < m1_lower_bb) bearScore += 1;

        const netScore = bullScore - bearScore;
        let direction = 'NEUTRAL';
        let emoji = '⚖️';
        let color = '#94a3b8';
        let bgColor = 'rgba(148,163,184,0.08)';
        let borderColor = 'rgba(148,163,184,0.2)';
        let confidence = 'Low';
        let detail = '';

        if (netScore >= 4) {
          direction = 'STRONG BULLISH'; emoji = '🟢🔥'; color = '#34d399'; confidence = 'High';
          bgColor = 'rgba(52,211,153,0.08)'; borderColor = 'rgba(52,211,153,0.25)';
          detail = `Price is running above all M5 EMAs with strong RSI momentum. The M5 trend is fully aligned bullish (EMA9 > EMA21 > EMA50). Look for pullback entries on dips toward EMA21.`;
        } else if (netScore >= 2) {
          direction = 'BULLISH'; emoji = '🟢'; color = '#34d399'; confidence = 'Moderate';
          bgColor = 'rgba(52,211,153,0.06)'; borderColor = 'rgba(52,211,153,0.18)';
          detail = `M5 structure is leaning bullish. EMAs are starting to fan upward and price is holding above key moving averages. Watch for pullback-to-EMA entries on the long side.`;
        } else if (netScore <= -4) {
          direction = 'STRONG BEARISH'; emoji = '🔴🔥'; color = '#f87171'; confidence = 'High';
          bgColor = 'rgba(248,113,113,0.08)'; borderColor = 'rgba(248,113,113,0.25)';
          detail = `Price is trading below all M5 EMAs with falling RSI. The M5 trend is fully aligned bearish (EMA9 < EMA21 < EMA50). Look for short entries on rallies into EMA21 resistance.`;
        } else if (netScore <= -2) {
          direction = 'BEARISH'; emoji = '🔴'; color = '#f87171'; confidence = 'Moderate';
          bgColor = 'rgba(248,113,113,0.06)'; borderColor = 'rgba(248,113,113,0.18)';
          detail = `M5 structure is leaning bearish. EMAs are compressing or fanning downward. Favor short setups and be cautious with longs.`;
        } else {
          direction = 'NEUTRAL / RANGING'; emoji = '⚖️'; color = '#94a3b8'; confidence = 'Low';
          detail = `No clear directional bias detected. The M5 EMAs are flat or mixed, and RSI is hovering near midline. This is a choppy / consolidation zone — mean reversion (BB) strategies are favored over trend-following entries.`;
        }

        // Recent trade activity summary
        const recentOpen = trades.filter(t => t.status === 'OPEN');
        const recentLongs = recentOpen.filter(t => t.direction === 'LONG').length;
        const recentShorts = recentOpen.filter(t => t.direction === 'SHORT').length;

        return (
          <div style={{
            background: bgColor,
            border: `1px solid ${borderColor}`,
            borderRadius: 'var(--radius-lg)',
            padding: '18px 24px',
            marginBottom: 20,
            transition: 'all 0.3s ease'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <span style={{ fontSize: 22 }}>{emoji}</span>
              <span style={{ fontSize: 17, fontWeight: 800, color }}>
                {direction}
              </span>
              <span style={{
                fontSize: 11, fontWeight: 600, padding: '3px 10px',
                borderRadius: 6, background: `${color}22`, color,
                border: `1px solid ${color}44`
              }}>
                {confidence} Confidence
              </span>
              {recentOpen.length > 0 && (
                <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 'auto' }}>
                  Open positions: {recentLongs > 0 ? `${recentLongs} Long` : ''}{recentLongs > 0 && recentShorts > 0 ? ' · ' : ''}{recentShorts > 0 ? `${recentShorts} Short` : ''}
                </span>
              )}
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: 13, lineHeight: 1.5, margin: 0 }}>
              {detail}
            </p>
            <div style={{
              display: 'flex', gap: 16, marginTop: 10, fontSize: 11, color: 'var(--text-muted)'
            }}>
              <span>M5 RSI: <b style={{ color: m5_rsi > 65 ? '#34d399' : m5_rsi < 35 ? '#f87171' : 'var(--text-secondary)' }}>{m5_rsi.toFixed(1)}</b></span>
              <span>M1 RSI: <b style={{ color: m1_rsi > 65 ? '#34d399' : m1_rsi < 35 ? '#f87171' : 'var(--text-secondary)' }}>{m1_rsi.toFixed(1)}</b></span>
              <span>EMA Spread: <b style={{ color: 'var(--text-secondary)' }}>{(m5_ema9 - m5_ema50).toFixed(2)}</b></span>
              {m5_atr !== undefined && <span>ATR: <b style={{ color: 'var(--text-secondary)' }}>{m5_atr.toFixed(2)}</b></span>}
            </div>
          </div>
        );
      })()}

      {/* Trade Ledger */}
      <div style={{
        background: 'var(--bg-card)', borderRadius: 'var(--radius-lg)',
        border: '1px solid var(--border-subtle)', overflow: 'hidden',
        backdropFilter: 'blur(12px)'
      }}>
        <div style={{
          padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)',
          background: 'rgba(15,23,42,0.5)', display: 'flex', justifyContent: 'space-between', alignItems: 'center'
        }}>
          <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-secondary)' }}>Trade Ledger</span>
          {loading && <span style={{ color: 'var(--accent-green)', fontSize: 12 }} className="animate-pulse">Syncing...</span>}
        </div>

        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: 'rgba(15,23,42,0.6)', color: 'var(--text-muted)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.8 }}>
                <th style={{ padding: '12px 10px', textAlign: 'center', width: 40 }}>#</th>
                <th style={{ padding: '12px 16px', textAlign: 'left' }}>Time</th>
                <th style={{ padding: '12px 16px', textAlign: 'left' }}>Dir</th>
                <th style={{ padding: '12px 16px', textAlign: 'left' }}>Strategy</th>
                <th style={{ padding: '12px 16px', textAlign: 'right' }}>Entry</th>
                <th style={{ padding: '12px 16px', textAlign: 'right' }}>Exit / Live</th>
                <th style={{ padding: '12px 16px', textAlign: 'center' }}>Peak / Low</th>
                <th style={{ padding: '12px 16px', textAlign: 'center' }}>TP / SL</th>
                <th style={{ padding: '12px 16px', textAlign: 'center' }}>Duration</th>
                <th style={{ padding: '12px 16px', textAlign: 'center' }}>Status</th>
                <th style={{ padding: '12px 16px', textAlign: 'right' }}>P/L</th>
                <th style={{ padding: '12px 10px', textAlign: 'center' }}></th>
              </tr>
            </thead>
            <tbody>
              {trades.length === 0 && !loading && (
                <tr>
                  <td colSpan={12} style={{ padding: '40px 20px', textAlign: 'center', color: 'var(--text-dim)', fontStyle: 'italic' }}>
                    Waiting for trading signals...
                  </td>
                </tr>
              )}
              {trades.map((trade, index) => {
                const unrealized = getUnrealized(trade);
                const pnl = trade.status === 'CLOSED' ? trade.pnl : unrealized;
                return (
                  <React.Fragment key={trade._id}>
                    <tr
                      style={{
                        borderBottom: trade.meta ? 'none' : '1px solid rgba(51,65,85,0.3)',
                        transition: 'background 0.15s'
                      }}
                      onMouseEnter={e => (e.currentTarget.style.background = 'rgba(15,23,42,0.4)')}
                      onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                    >
                      <td style={{ padding: '12px 10px', textAlign: 'center', color: 'var(--text-dim)', fontWeight: 600, fontSize: 11 }}>
                        {trades.length - index}
                      </td>
                      <td style={{ padding: '12px 16px' }}>
                        <div style={{ color: 'var(--text-primary)', fontSize: 12, fontWeight: 500 }}>
                          {new Date(trade.entryTime).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                        </div>
                        <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                          {new Date(trade.entryTime).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                        </div>
                      </td>
                      <td style={{ padding: '12px 16px' }}>
                        <span style={{
                          padding: '3px 10px', borderRadius: 4, fontSize: 11, fontWeight: 700,
                          background: trade.direction === 'LONG' ? 'var(--accent-green-bg)' : 'var(--accent-red-bg)',
                          color: trade.direction === 'LONG' ? 'var(--accent-green)' : 'var(--accent-red)',
                          border: `1px solid ${trade.direction === 'LONG' ? 'var(--accent-green-border)' : 'var(--accent-red-border)'}`
                        }}>
                          {trade.direction === 'LONG' ? '🟢 LONG' : '🔴 SHORT'}
                        </span>
                      </td>
                      <td style={{ padding: '12px 16px' }}>
                        <span style={{
                          fontSize: 11, fontWeight: 600, color: strategyColor(trade.signalType || ''),
                          background: `${strategyColor(trade.signalType || '')}18`,
                          padding: '2px 8px', borderRadius: 4
                        }}>
                          {trade.signalType || 'N/A'}
                        </span>
                      </td>
                      <td style={{ padding: '12px 16px', textAlign: 'right', color: 'var(--text-primary)', fontWeight: 500, fontFamily: 'monospace' }}>
                        ${trade.entryPrice.toFixed(2)}
                      </td>
                      <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                        {trade.status === 'OPEN' && livePrice ? (
                          <>
                            <div style={{
                              color: (trade.direction === 'LONG' ? livePrice >= trade.entryPrice : livePrice <= trade.entryPrice) ? 'var(--accent-green)' : 'var(--accent-red)',
                              fontWeight: 600, fontFamily: 'monospace'
                            }} className="animate-pulse">
                              ${livePrice.toFixed(2)}
                            </div>
                            <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>LIVE</div>
                          </>
                        ) : trade.exitPrice ? (
                          <span style={{ color: 'var(--text-secondary)', fontFamily: 'monospace' }}>${trade.exitPrice.toFixed(2)}</span>
                        ) : '--'}
                      </td>
                      <td style={{ padding: '12px 16px', textAlign: 'center', fontSize: 11, fontFamily: 'monospace', color: 'var(--text-muted)' }}>
                        {trade.peakProfit !== undefined && trade.peakLoss !== undefined ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                            <span style={{ color: 'var(--accent-green)' }}>
                              +${trade.peakProfit.toFixed(2)}
                            </span>
                            <span style={{ color: 'var(--accent-red)' }}>
                              -${Math.abs(trade.peakLoss).toFixed(2)}
                            </span>
                          </div>
                        ) : '--'}
                      </td>
                      <td style={{ padding: '12px 16px', textAlign: 'center', fontSize: 11 }}>
                        {trade.tp && trade.sl ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                            <span style={{ color: 'var(--accent-green)' }}>+${Math.abs(trade.tp - trade.entryPrice).toFixed(2)}</span>
                            <span style={{ color: 'var(--accent-red)' }}>-${Math.abs(trade.sl - trade.entryPrice).toFixed(2)}</span>
                          </div>
                        ) : '--'}
                      </td>
                      <td style={{ padding: '12px 16px', textAlign: 'center', color: 'var(--text-secondary)', fontSize: 12 }}>
                        {formatDuration(trade.entryTime, trade.exitTime)}
                      </td>
                      <td style={{ padding: '12px 16px', textAlign: 'center' }}>
                        {trade.status === 'PREPARE' ? (
                          <span style={{ color: 'var(--accent-yellow)', fontSize: 11, fontWeight: 700 }} className="animate-pulse">⏳ PREPARE</span>
                        ) : trade.status === 'EXPIRED' ? (
                          <span style={{ color: 'var(--text-dim)', fontSize: 11, fontWeight: 600 }}>⌛ EXPIRED</span>
                        ) : trade.status === 'OPEN' ? (
                          <span style={{ color: 'var(--accent-blue)', fontSize: 11, fontWeight: 700 }}>● LIVE</span>
                        ) : trade.closeReason === 'TAKE_PROFIT' ? (
                          <span style={{ color: 'var(--accent-green)', fontSize: 11, fontWeight: 600 }}>✅ TP</span>
                        ) : (
                          <span style={{ color: 'var(--accent-red)', fontSize: 11, fontWeight: 600 }}>❌ SL</span>
                        )}
                      </td>
                      <td style={{
                        padding: '12px 16px', textAlign: 'right', fontWeight: 700, fontFamily: 'monospace',
                        color: pnl && pnl > 0 ? 'var(--accent-green)' : pnl && pnl < 0 ? 'var(--accent-red)' : 'var(--text-muted)'
                      }}>
                        {trade.status === 'OPEN' ? (
                          unrealized !== null ? (
                            <span className="animate-pulse">{unrealized > 0 ? '+' : ''}${unrealized.toFixed(2)}</span>
                          ) : '--'
                        ) : (
                          <span>{trade.pnl! > 0 ? '+' : ''}${trade.pnl?.toFixed(2)}</span>
                        )}
                      </td>
                      <td style={{ padding: '12px 10px', textAlign: 'center' }}>
                        <button
                          onClick={() => handleDelete(trade._id)}
                          style={{
                            background: 'transparent', border: '1px solid rgba(248,113,113,0.2)',
                            color: 'var(--accent-red)', borderRadius: 6, fontSize: 11, padding: '4px 8px',
                            cursor: 'pointer', opacity: 0.6, transition: 'opacity 0.2s'
                          }}
                          onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                          onMouseLeave={e => (e.currentTarget.style.opacity = '0.6')}
                          title="Delete this trade"
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                    {/* Meta row: show strategy conditions */}
                    {trade.meta && trade.meta.rule && (
                      <tr>
                        <td colSpan={12} style={{
                          padding: '4px 16px 12px 46px', fontSize: 11, color: 'var(--text-secondary)',
                          fontStyle: 'italic', borderBottom: '1px solid rgba(51,65,85,0.3)'
                        }}>
                          💡 <b style={{ color: 'var(--text-primary)' }}>Rule:</b> {trade.meta.rule}
                          {trade.meta.m1_rsi !== undefined && <> · <b>RSI:</b> {typeof trade.meta.m1_rsi === 'number' ? trade.meta.m1_rsi.toFixed(1) : trade.meta.m1_rsi}</>}
                          {trade.meta.m5_atr !== undefined && <> · <b>ATR:</b> {typeof trade.meta.m5_atr === 'number' ? trade.meta.m5_atr.toFixed(3) : trade.meta.m5_atr}</>}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Footer */}
      <div style={{ textAlign: 'center', marginTop: 24, fontSize: 11, color: 'var(--text-dim)' }}>
        XAU/USD Paper Trading Engine · Strategies: EMA Pullback / BB Reversion / Institutional Breakout
      </div>
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={{
      background: 'rgba(15,23,42,0.8)', padding: '10px 20px', borderRadius: 10,
      border: '1px solid var(--border-subtle)', minWidth: 90, textAlign: 'center'
    }}>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 800, color, marginTop: 2, fontFamily: 'monospace' }}>{value}</div>
    </div>
  );
}

function IndicatorCard({ label, value, warn }: { label: string; value?: number; warn?: boolean }) {
  return (
    <div style={{
      background: warn ? 'rgba(251,191,36,0.08)' : 'rgba(15,23,42,0.5)',
      padding: '10px 12px', borderRadius: 'var(--radius)',
      border: `1px solid ${warn ? 'rgba(251,191,36,0.3)' : 'var(--border-subtle)'}`,
      textAlign: 'center'
    }}>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</div>
      <div style={{
        fontSize: 14, fontWeight: 700, marginTop: 4, fontFamily: 'monospace',
        color: value !== undefined ? (warn ? 'var(--accent-yellow)' : 'var(--text-primary)') : 'var(--text-dim)'
      }}>
        {value !== undefined ? (label.includes('RSI') ? value.toFixed(1) : value.toFixed(3)) : '—'}
      </div>
    </div>
  );
}
