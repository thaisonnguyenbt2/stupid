/**
 * XAU/USD Backtester — Frontend Application
 * 
 * Connects to the FastAPI WebSocket backend, renders candlestick charts
 * with trade markers and EMA overlays using TradingView Lightweight Charts.
 */

// ===================== STATE =====================
let ws = null;
let chart = null;
let candleSeries = null;
let ema9Series = null;
let ema21Series = null;
let ema50Series = null;
let bbUpperSeries = null;
let bbLowerSeries = null;
let markers = [];
let activeLines = [];  // Track Entry/TP/SL price lines
let trades = [];
let currentSpeed = 100;
let isPlaying = false;
let selectedSlot = 'ALL';
let selectedTradeId = null;

// Candle aggregation for higher timeframes
let m1Candles = [];
let displayTf = 'M5';
let tfMinutes = { M1: 1, M5: 5, M15: 15 };
let currentDisplayCandle = null;

// ===================== CHART INIT =====================
function initChart() {
  const container = document.getElementById('chart');
  chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight,
    layout: {
      background: { type: 'solid', color: '#0a0e17' },
      textColor: '#94a3b8',
      fontSize: 12,
      fontFamily: "'Inter', system-ui",
    },
    grid: {
      vertLines: { color: 'rgba(42, 53, 72, 0.4)' },
      horzLines: { color: 'rgba(42, 53, 72, 0.4)' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: 'rgba(59, 130, 246, 0.3)', width: 1 },
      horzLine: { color: 'rgba(59, 130, 246, 0.3)', width: 1 },
    },
    rightPriceScale: {
      borderColor: '#2a3548',
      scaleMargins: { top: 0.1, bottom: 0.1 },
    },
    timeScale: {
      borderColor: '#2a3548',
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 12,
      barSpacing: 6,
    },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: '#22c55e',
    downColor: '#ef4444',
    borderUpColor: '#22c55e',
    borderDownColor: '#ef4444',
    wickUpColor: '#22c55e',
    wickDownColor: '#ef4444',
  });

  ema9Series = chart.addLineSeries({
    color: '#f59e0b',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  ema21Series = chart.addLineSeries({
    color: '#3b82f6',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  ema50Series = chart.addLineSeries({
    color: '#a855f7',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  bbUpperSeries = chart.addLineSeries({
    color: 'rgba(14, 165, 233, 0.8)',
    lineWidth: 2,
    lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  bbLowerSeries = chart.addLineSeries({
    color: 'rgba(14, 165, 233, 0.8)',
    lineWidth: 2,
    lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  // Resize handler
  const ro = new ResizeObserver(() => {
    chart.applyOptions({
      width: container.clientWidth,
      height: container.clientHeight,
    });
  });
  ro.observe(container);
}

// ===================== CANDLE AGGREGATION =====================
function getDisplayBucket(ts) {
  const mins = tfMinutes[displayTf] || 5;
  return Math.floor(ts / (mins * 60)) * (mins * 60);
}

function processM1Candle(candle) {
  if (displayTf === 'M1') {
    // Direct pass-through
    candleSeries.update({
      time: candle.time,
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    });
    return;
  }

  // Aggregate M1 into display timeframe
  const bucket = getDisplayBucket(candle.time);

  if (!currentDisplayCandle || currentDisplayCandle.time !== bucket) {
    // Finalize previous candle
    if (currentDisplayCandle) {
      candleSeries.update(currentDisplayCandle);
    }
    // Start new candle
    currentDisplayCandle = {
      time: bucket,
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    };
  } else {
    // Update current candle
    currentDisplayCandle.high = Math.max(currentDisplayCandle.high, candle.high);
    currentDisplayCandle.low = Math.min(currentDisplayCandle.low, candle.low);
    currentDisplayCandle.close = candle.close;
  }

  candleSeries.update(currentDisplayCandle);
}

// ===================== WEBSOCKET =====================
function connect() {
  const startDate = document.getElementById('startDate').value;
  displayTf = document.getElementById('timeframe').value;

  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${protocol}://${location.host}/ws/replay`);

  ws.onopen = () => {
    console.log('[WS] Connected');
    ws.send(JSON.stringify({
      action: 'start',
      date: startDate,
      speed: currentSpeed,
      tf: displayTf,
      mode: document.getElementById('tradeMode').value,
      rr: document.getElementById('rrSlot').value,
    }));
    setPlayingState(true);
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleMessage(msg);
  };

  ws.onclose = () => {
    console.log('[WS] Disconnected');
    setPlayingState(false);
  };

  ws.onerror = (err) => {
    console.error('[WS] Error:', err);
  };
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'candle':
      processM1Candle(msg.data);
      // Update EMA lines
      if (msg.data.ema9) {
        const bucket = displayTf === 'M1' ? msg.data.time : getDisplayBucket(msg.data.time);
        ema9Series.update({ time: bucket, value: msg.data.ema9 });
        ema21Series.update({ time: bucket, value: msg.data.ema21 });
        ema50Series.update({ time: bucket, value: msg.data.ema50 });
        if (msg.data.upper_bb) {
          bbUpperSeries.update({ time: bucket, value: msg.data.upper_bb });
          bbLowerSeries.update({ time: bucket, value: msg.data.lower_bb });
        }
      }
      break;

    case 'trade_open':
      addTrade(msg.data);
      addTradeMarker(msg.data);
      break;

    case 'trade_close':
      updateTrade(msg.data);
      break;

    case 'stats':
      updateStats(msg.data);
      break;

    case 'tick_time':
      updateTickTime(msg.data);
      break;

    case 'warmup_done':
      console.log('[Replay] Warmup done:', msg.data);
      break;

    case 'done':
      console.log('[Replay] Complete:', msg.data);
      setPlayingState(false);
      break;
  }
}

// ===================== TRADE MANAGEMENT =====================
function addTrade(trade) {
  trades.push(trade); // Add to bottom (chronological order)
  renderTradeList();
}

function updateTrade(closedTrade) {
  const idx = trades.findIndex(t => t.id === closedTrade.id);
  if (idx !== -1) {
    trades[idx] = { ...trades[idx], ...closedTrade };
  }

  // Add exit marker on chart
  if (closedTrade.exit_time) {
    const exitTs = Math.floor(new Date(closedTrade.exit_time).getTime() / 1000);
    const bucket = displayTf === 'M1' ? exitTs : getDisplayBucket(exitTs);
    const isWin = closedTrade.status === 'WIN';
    const isLong = closedTrade.direction === 'LONG';

    markers.push({
      time: bucket,
      position: isLong ? 'aboveBar' : 'belowBar',
      color: isWin ? '#22c55e' : '#ef4444',
      shape: 'circle',
      text: isWin ? '✓' : '✗',
      id: closedTrade.id + 100000,
    });

    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  }

  renderTradeList();
}

function addTradeMarker(trade) {
  const bucket = displayTf === 'M1' ? Math.floor(new Date(trade.entry_time).getTime() / 1000) : getDisplayBucket(Math.floor(new Date(trade.entry_time).getTime() / 1000));
  const isLong = trade.direction === 'LONG';

  markers.push({
    time: bucket,
    position: isLong ? 'belowBar' : 'aboveBar',
    color: isLong ? '#22c55e' : '#ef4444',
    shape: isLong ? 'arrowUp' : 'arrowDown',
    text: `${trade.slot} ${trade.direction}`,
    id: trade.id,
  });

  // Sort markers by time (required by Lightweight Charts)
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
}

function renderTradeList() {
  const list = document.getElementById('tradeList');
  const scrollTop = list.scrollTop;  // Preserve scroll position
  const filtered = selectedSlot === 'ALL' ? trades : trades.filter(t => t.slot === selectedSlot);

  document.getElementById('tradeCount').textContent = `${filtered.length} trades`;

  // Only render last 100 for performance
  const visible = filtered.slice(0, 100);

  list.innerHTML = visible.map(t => {
    const dirClass = t.direction === 'LONG' ? 'long' : 'short';
    const resultClass = t.status === 'WIN' ? 'win' : (t.status === 'LOSS' ? 'loss' : 'open');
    const resultText = t.status === 'OPEN' ? '⏳ OPEN' : (t.status === 'WIN' ? '✅ WIN' : '❌ LOSS');
    const pnlText = t.pnl !== undefined ? `$${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}` : '—';
    const pnlClass = t.pnl >= 0 ? 'positive' : 'negative';
    const selected = t.id === selectedTradeId ? ' trade-card--selected' : '';
    const tpDist = t.tp_dist !== undefined ? `+$${t.tp_dist.toFixed(2)}` : `$${t.tp.toFixed(2)}`;
    const slDist = t.sl_dist !== undefined ? `-$${t.sl_dist.toFixed(2)}` : `$${t.sl.toFixed(2)}`;
    const atrText = t.atr ? `ATR: $${t.atr.toFixed(2)}` : '';

    return `
      <div class="trade-card${selected}" data-id="${t.id}" onclick="selectTrade(${t.id})">
        <div class="trade-card__header">
          <span class="trade-card__id">#${t.id} · ${t.strategy} ${atrText}</span>
          <span class="trade-card__result trade-card__result--${resultClass}">${resultText}</span>
        </div>
        <div class="trade-card__details">
          <span><span class="trade-card__dir trade-card__dir--${dirClass}">${t.direction}</span> $${t.entry_price.toFixed(2)}</span>
          <span style="color:var(--accent-green)">TP: ${tpDist}</span>
          <span>${t.entry_time?.split('T')[1]?.substring(0, 8) || ''}</span>
          <span style="color:var(--accent-red)">SL: ${slDist}</span>
        </div>
        ${t.pnl !== undefined ? `<div class="trade-card__pnl trade-card__pnl--${pnlClass}">${pnlText}</div>` : ''}
      </div>
    `;
  }).join('');

  // Restore scroll position so new trades don't disrupt viewing
  list.scrollTop = scrollTop;
}

function clearTradeLines() {
  activeLines.forEach(line => {
    try { candleSeries.removePriceLine(line); } catch (e) { }
  });
  activeLines = [];
}

function selectTrade(id) {
  selectedTradeId = id;
  clearTradeLines();

  const trade = trades.find(t => t.id === id);
  if (trade) {
    const ts = Math.floor(new Date(trade.entry_time).getTime() / 1000);
    const bucket = displayTf === 'M1' ? ts : getDisplayBucket(ts);

    // Navigate chart to the trade's candle
    chart.timeScale().scrollToRealTime();
    setTimeout(() => {
      chart.timeScale().setVisibleRange({
        from: bucket - 1800,  // 30 min before
        to: bucket + 3600,    // 60 min after
      });

      // Highlight the entry candle with a marker
      highlightCandle(bucket, trade);
    }, 50);

    // Draw Entry price line (blue)
    const entryLine = candleSeries.createPriceLine({
      price: trade.entry_price,
      color: '#3b82f6',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: `Entry $${trade.entry_price.toFixed(2)}`,
    });
    activeLines.push(entryLine);

    // Draw TP line (green)
    const tpLabel = trade.tp_dist ? `TP +$${trade.tp_dist.toFixed(2)}` : `TP $${trade.tp.toFixed(2)}`;
    const tpLine = candleSeries.createPriceLine({
      price: trade.tp,
      color: '#22c55e',
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: tpLabel,
    });
    activeLines.push(tpLine);

    // Draw SL line (red)
    const slLabel = trade.sl_dist ? `SL -$${trade.sl_dist.toFixed(2)}` : `SL $${trade.sl.toFixed(2)}`;
    const slLine = candleSeries.createPriceLine({
      price: trade.sl,
      color: '#ef4444',
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: slLabel,
    });
    activeLines.push(slLine);
  }
  renderTradeList();
}

// Highlight selected trade on chart — uses a dedicated marker that stays anchored
let highlightMarkerId = null;

function highlightCandle(time, trade) {
  // Remove previous highlight marker
  removeHighlight();

  // Add a prominent highlight marker at the entry
  highlightMarkerId = trade.id + 200000;
  const isLong = trade.direction === 'LONG';
  markers.push({
    time: time,
    position: 'inBar',
    color: '#3b82f6',
    shape: 'square',
    text: '',
    id: highlightMarkerId,
  });
  markers.sort((a, b) => a.time - b.time);

  // Update with highlighted entry marker
  const updatedMarkers = markers.map(m => ({
    ...m,
    color: m.id === trade.id ? '#3b82f6' :
      m.id === highlightMarkerId ? 'rgba(59,130,246,0.4)' :
        (m.shape === 'arrowUp' ? '#22c55e' : m.shape === 'circle' ? m.color : '#ef4444'),
    size: m.id === trade.id ? 3 : (m.id === highlightMarkerId ? 3 : 1),
  }));
  candleSeries.setMarkers(updatedMarkers);
}

function removeHighlight() {
  if (highlightMarkerId) {
    markers = markers.filter(m => m.id !== highlightMarkerId);
    highlightMarkerId = null;
  }
}

// ===================== STATS =====================
function updateStats(stats) {
  document.getElementById('statTrades').textContent = stats.total;
  document.getElementById('statWins').textContent = stats.wins;
  document.getElementById('statLosses').textContent = stats.losses;
  document.getElementById('statWinRate').textContent = `${stats.winRate}%`;

  const pnlEl = document.getElementById('statPnl');
  pnlEl.textContent = `$${stats.pnl >= 0 ? '+' : ''}${stats.pnl.toFixed(2)}`;
  pnlEl.className = `stat-card__value ${stats.pnl >= 0 ? 'stat-card__value--green' : 'stat-card__value--red'}`;
}

function updateTickTime(data) {
  document.getElementById('currentTime').textContent = data.time;
  document.getElementById('currentPrice').textContent = `$${data.price}`;
}

// ===================== CONTROLS =====================
function setPlayingState(playing) {
  isPlaying = playing;
  document.getElementById('btnPlay').style.display = playing ? 'none' : '';
  document.getElementById('btnPause').style.display = playing ? '' : 'none';
  document.getElementById('btnStop').style.display = playing ? '' : 'none';
  document.getElementById('statusDot').className = playing ? 'status-dot status-dot--active' : 'status-dot';
}

function setSpeed(speed) {
  currentSpeed = speed;
  // Update button styles
  document.querySelectorAll('#speedBtns .btn').forEach(btn => {
    const btnSpeed = btn.dataset.speed;
    btn.className = `btn btn--small${(btnSpeed === String(speed)) ? ' btn--active' : ''}`;
  });
  // Send to backend
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'speed', speed }));
  }
}

// ===================== EVENT HANDLERS =====================
document.getElementById('btnPlay').addEventListener('click', () => {
  // Clear previous state
  markers = [];
  clearTradeLines();
  trades = [];
  currentDisplayCandle = null;
  m1Candles = [];

  if (candleSeries) {
    candleSeries.setData([]);
    candleSeries.setMarkers([]);
    ema9Series.setData([]);
    ema21Series.setData([]);
    ema50Series.setData([]);
    bbUpperSeries.setData([]);
    bbLowerSeries.setData([]);
  }

  renderTradeList();
  updateStats({ total: 0, wins: 0, losses: 0, winRate: 0, pnl: 0 });

  connect();
});

document.getElementById('btnPause').addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'pause' }));
    document.getElementById('btnPause').textContent = '▶ Resume';
    document.getElementById('btnPause').onclick = () => {
      ws.send(JSON.stringify({ action: 'resume' }));
      document.getElementById('btnPause').textContent = '⏸ Pause';
      document.getElementById('btnPause').onclick = null;
      document.getElementById('btnPause').addEventListener('click', arguments.callee);
    };
  }
});

document.getElementById('btnStop').addEventListener('click', () => {
  if (ws) {
    ws.send(JSON.stringify({ action: 'stop' }));
    ws.close();
  }
  setPlayingState(false);
});

// Dry Run — background job with progress polling
let dryRunData = null;
let dryRunPollTimer = null;

document.getElementById('btnDryRun').addEventListener('click', async () => {
  const startDate = document.getElementById('startDate').value;
  const endDate = document.getElementById('endDate').value;

  // Clear state
  markers = [];
  clearTradeLines();
  trades = [];
  renderTradeList();
  updateStats({ total: 0, wins: 0, losses: 0, winRate: 0, pnl: 0 });
  dryRunData = null;
  document.getElementById('btnExport').style.display = 'none';

  // Show loading state
  const btn = document.getElementById('btnDryRun');
  btn.textContent = '■ Cancel';
  btn.className = 'btn';
  btn.dataset.mode = 'cancel';
  document.getElementById('currentTime').textContent = 'Starting dry run...';
  document.getElementById('currentPrice').textContent = '';
  document.getElementById('statusDot').className = 'status-dot status-dot--active';

  try {
    const tradeMode = document.getElementById('tradeMode').value;
    const rrSlot = document.getElementById('rrSlot').value;
    let url = `/api/dryrun?start=${startDate}&mode=${tradeMode}&rr=${rrSlot}`;
    if (endDate) url += `&end=${endDate}`;

    const res = await fetch(url, { method: 'POST' });
    const data = await res.json();

    if (data.error) {
      document.getElementById('currentTime').textContent = data.error;
      resetDryRunBtn();
      return;
    }

    // Start polling for progress
    startDryRunPolling();
  } catch (err) {
    console.error('[DryRun] Error:', err);
    document.getElementById('currentTime').textContent = 'Error: ' + err.message;
    resetDryRunBtn();
  }
});

function resetDryRunBtn() {
  const btn = document.getElementById('btnDryRun');
  btn.textContent = '⚡ Dry Run';
  btn.className = 'btn btn--primary';
  btn.dataset.mode = 'start';
  document.getElementById('statusDot').className = 'status-dot';
}

function startDryRunPolling() {
  if (dryRunPollTimer) clearInterval(dryRunPollTimer);

  dryRunPollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/dryrun/status');
      const data = await res.json();
      const p = data.progress;

      if (p && p.ticks > 0) {
        document.getElementById('currentTime').textContent =
          `📍 ${p.current_date} · ${(p.ticks / 1000000).toFixed(1)}M ticks · ${p.candles.toLocaleString()} candles · ⏱ ${p.elapsed}s`;
        document.getElementById('currentPrice').textContent =
          `${p.trades} trades · ${p.winRate}% WR · $${p.pnl >= 0 ? '+' : ''}${p.pnl.toFixed(2)}`;

        // Update stats panel live
        updateStats({
          total: p.trades,
          wins: p.wins,
          losses: p.losses,
          winRate: p.winRate,
          pnl: p.pnl,
        });
      }

      if (data.done) {
        clearInterval(dryRunPollTimer);
        dryRunPollTimer = null;
        await fetchDryRunResult();
      }

      if (!data.running && !data.done) {
        clearInterval(dryRunPollTimer);
        dryRunPollTimer = null;
        document.getElementById('currentTime').textContent = 'Dry run cancelled.';
        resetDryRunBtn();
      }
    } catch (err) {
      console.error('[Poll] Error:', err);
    }
  }, 1000);
}

async function fetchDryRunResult() {
  try {
    const res = await fetch('/api/dryrun/result');
    dryRunData = await res.json();

    if (dryRunData.error) {
      document.getElementById('currentTime').textContent = dryRunData.error;
      resetDryRunBtn();
      return;
    }

    trades = dryRunData.trades;
    renderTradeList();
    updateStats(dryRunData.stats);

    document.getElementById('currentTime').textContent =
      `✅ Done in ${dryRunData.elapsed_seconds}s · ${dryRunData.ticks.toLocaleString()} ticks · ${dryRunData.candles.toLocaleString()} candles`;
    document.getElementById('currentPrice').textContent = '';

    document.getElementById('btnExport').style.display = '';
    resetDryRunBtn();

    console.log('[DryRun] Result:', dryRunData);
  } catch (err) {
    console.error('[DryRun] Fetch result error:', err);
  }
}

// Handle cancel click (reuse dry run button)
document.getElementById('btnDryRun').addEventListener('click', async (e) => {
  if (e.currentTarget.dataset.mode === 'cancel') {
    e.stopImmediatePropagation();
    await fetch('/api/dryrun/cancel', { method: 'POST' });
    if (dryRunPollTimer) { clearInterval(dryRunPollTimer); dryRunPollTimer = null; }
    document.getElementById('currentTime').textContent = 'Dry run cancelled.';
    resetDryRunBtn();
  }
}, true);

// Export trades to JSON
document.getElementById('btnExport').addEventListener('click', () => {
  if (!dryRunData) return;

  const exportData = {
    generated_at: new Date().toISOString(),
    config: {
      slots: [{ name: 'A', tp_mult: 3.0, sl_mult: 1.0 }],
      spread_offset: 0.5,
    },
    stats: dryRunData.stats,
    daily_pnl: dryRunData.daily_pnl,
    elapsed_seconds: dryRunData.elapsed_seconds,
    ticks_processed: dryRunData.ticks,
    candles_processed: dryRunData.candles,
    trades: dryRunData.trades,
  };

  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `backtest_${document.getElementById('startDate').value}_${document.getElementById('endDate').value || 'all'}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

// Speed buttons
document.querySelectorAll('#speedBtns .btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const speed = btn.dataset.speed === 'MAX' ? 'MAX' : parseInt(btn.dataset.speed);
    setSpeed(speed);
  });
});

// Slot tabs
document.querySelectorAll('.slot-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.slot-tab').forEach(t => t.classList.remove('slot-tab--active'));
    tab.classList.add('slot-tab--active');
    selectedSlot = tab.dataset.slot;
    renderTradeList();
  });
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  const speedMap = { '1': 1, '2': 5, '3': 10, '4': 100, '5': 200, '6': 400, '7': 'MAX' };
  if (speedMap[e.key]) {
    setSpeed(speedMap[e.key]);
  }
  if (e.key === ' ') {
    e.preventDefault();
    if (isPlaying) {
      document.getElementById('btnPause').click();
    } else {
      document.getElementById('btnPlay').click();
    }
  }
});

// ===================== INIT =====================
initChart();

// Click chart to deselect trade and refresh list
document.getElementById('chart').addEventListener('click', () => {
  if (selectedTradeId) {
    selectedTradeId = null;
    clearTradeLines();
    removeHighlight();
    // Restore original marker colors
    candleSeries.setMarkers(markers);
    renderTradeList();
  }
});
