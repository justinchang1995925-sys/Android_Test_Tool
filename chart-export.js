(function () {
  const theme = {
    bg: "#060b14",
    panel: "#0f172a",
    text: "#e2e8f0",
    muted: "#94a3b8",
    grid: "rgba(71, 85, 105, 0.65)",
    gridBand: "rgba(255, 255, 255, 0.035)",
    chartTop: "#131c2e",
    chartBottom: "#0a101c",
    threshold: "#f87171",
    thresholdSoft: "rgba(248, 113, 113, 0.12)",
    tooltipBg: "rgba(2, 6, 23, 0.96)",
    pointRing: "#1e293b",
    border: "rgba(51, 65, 85, 0.75)",
  };

  const layout = { padding: { top: 28, right: 24, bottom: 44, left: 58 }, gridLines: 5 };

  function mountChart(config) {
    const canvasId = config.canvasId || "chart";
    const tooltipId = config.tooltipId || "tooltip";
    const legendId = config.legendId || "legend";
    const canvas = document.getElementById(canvasId);
    const tooltip = document.getElementById(tooltipId);
    const legendEl = document.getElementById(legendId);
    if (!canvas || !tooltip || !legendEl) return;

    const wrap = canvas.parentElement;
    let hoverState = null;

    function parseTime(value) {
      return value instanceof Date ? value : new Date(value);
    }

    function formatTimeShort(date) {
      const d = parseTime(date);
      const pad = v => String(v).padStart(2, "0");
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function formatAxisValue(value, unit) {
      if (unit === "MB" && value >= 1024) return `${(value / 1024).toFixed(1)}G`;
      return `${value.toFixed(value >= 100 ? 0 : 1)}${unit === "%" ? "%" : ""}`;
    }

    function pointX(index, scale) {
      return scale.pad.left + (scale.plotW * index) / Math.max(1, config.sampleLimit - 1);
    }

    function valueY(value, scale) {
      return scale.pad.top + scale.plotH - ((value - scale.min) / (scale.max - scale.min)) * scale.plotH;
    }

    function fitCanvas() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = wrap.clientWidth;
      const height = 440;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      canvas._logical = { width, height };
      return ctx;
    }

    function buildScale(lines) {
      const { width, height } = canvas._logical;
      const pad = layout.padding;
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const allValues = [];
      for (const line of lines) {
        allValues.push(...line.points.map(p => p.value));
      }
      if (config.threshold != null) allValues.push(config.threshold);
      const max = Math.max(1, ...(allValues.length ? allValues : [1])) * 1.12;
      return { width, height, pad, plotW, plotH, min: 0, max };
    }

    function buildSmoothPath(coords) {
      if (coords.length < 2) return coords;
      const result = [coords[0]];
      for (let i = 0; i < coords.length - 1; i += 1) {
        const p0 = coords[Math.max(0, i - 1)];
        const p1 = coords[i];
        const p2 = coords[i + 1];
        const p3 = coords[Math.min(coords.length - 1, i + 2)];
        result.push({
          x: p1.x + (p2.x - p0.x) / 6,
          y: p1.y + (p2.y - p0.y) / 6,
          x2: p2.x - (p3.x - p1.x) / 6,
          y2: p2.y - (p3.y - p1.y) / 6,
          end: p2,
        });
      }
      return result;
    }

    function smoothPathTo(ctx, smooth) {
      ctx.moveTo(smooth[0].x, smooth[0].y);
      for (let i = 1; i < smooth.length; i += 1) {
        const seg = smooth[i];
        if (seg.end) ctx.bezierCurveTo(seg.x, seg.y, seg.x2, seg.y2, seg.end.x, seg.end.y);
      }
    }

    function drawPlotBackground(ctx, scale) {
      const { pad, plotW, plotH } = scale;
      const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
      grad.addColorStop(0, theme.chartTop);
      grad.addColorStop(1, theme.chartBottom);
      ctx.fillStyle = grad;
      ctx.fillRect(pad.left, pad.top, plotW, plotH);
      for (let i = 0; i < layout.gridLines; i += 1) {
        if (i % 2 === 0) continue;
        const y0 = pad.top + (plotH * i) / layout.gridLines;
        ctx.fillStyle = theme.gridBand;
        ctx.fillRect(pad.left, y0, plotW, plotH / layout.gridLines);
      }
    }

    function drawGridAndAxes(ctx, scale, lines) {
      const { width, height, pad, plotW, plotH, min, max } = scale;
      const unit = config.unit;
      ctx.strokeStyle = theme.grid;
      ctx.fillStyle = theme.muted;
      ctx.lineWidth = 1;
      ctx.font = "11px Segoe UI, sans-serif";
      for (let i = 0; i <= layout.gridLines; i += 1) {
        const y = pad.top + (plotH * i) / layout.gridLines;
        const value = max - ((max - min) * i) / layout.gridLines;
        ctx.beginPath();
        ctx.setLineDash(i === layout.gridLines ? [] : [4, 4]);
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + plotW, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(formatAxisValue(value, unit), pad.left - 10, y);
      }
      const refPoints = lines.find(l => l.points.length)?.points || [];
      if (refPoints.length >= 2) {
        const indices = [0, Math.floor((refPoints.length - 1) / 2), refPoints.length - 1];
        for (const idx of [...new Set(indices)]) {
          const x = pointX(idx, scale);
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillText(formatTimeShort(refPoints[idx].time), x, height - pad.bottom + 10);
        }
      } else if (refPoints.length === 1) {
        ctx.textAlign = "center";
        ctx.fillText(formatTimeShort(refPoints[0].time), pointX(0, scale), height - pad.bottom + 10);
      }
      ctx.strokeStyle = theme.border;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(pad.left, pad.top);
      ctx.lineTo(pad.left, pad.top + plotH);
      ctx.lineTo(pad.left + plotW, pad.top + plotH);
      ctx.stroke();
    }

    function drawThreshold(ctx, scale) {
      const threshold = config.threshold;
      if (threshold == null) return;
      const { pad, plotW, plotH } = scale;
      const y = valueY(threshold, scale);
      ctx.fillStyle = theme.thresholdSoft;
      ctx.fillRect(pad.left, pad.top, plotW, Math.max(0, y - pad.top));
      ctx.strokeStyle = theme.threshold;
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 6]);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = theme.threshold;
      ctx.font = "bold 11px Segoe UI, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      ctx.fillText(`阈值 ${formatAxisValue(threshold, config.unit)}`, pad.left + plotW - 4, y - 6);
    }

    function drawSeries(ctx, line, scale) {
      const points = line.points;
      if (!points.length) return;
      const coords = points.map((p, i) => ({
        x: pointX(i, scale),
        y: valueY(p.value, scale),
      }));
      if (points.length === 1) {
        ctx.fillStyle = line.stroke;
        ctx.beginPath();
        ctx.arc(coords[0].x, coords[0].y, 5, 0, Math.PI * 2);
        ctx.fill();
        return;
      }
      const smooth = buildSmoothPath(coords);
      const bottom = scale.pad.top + scale.plotH;
      ctx.save();
      ctx.beginPath();
      smoothPathTo(ctx, smooth);
      ctx.lineTo(coords[coords.length - 1].x, bottom);
      ctx.lineTo(coords[0].x, bottom);
      ctx.closePath();
      const areaGrad = ctx.createLinearGradient(0, scale.pad.top, 0, bottom);
      areaGrad.addColorStop(0, line.fill);
      areaGrad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = areaGrad;
      ctx.fill();
      ctx.restore();
      ctx.save();
      ctx.strokeStyle = line.stroke;
      ctx.lineWidth = 2.5;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      if (line.dashed) ctx.setLineDash([10, 6]);
      ctx.shadowColor = line.stroke;
      ctx.shadowBlur = 8;
      ctx.beginPath();
      smoothPathTo(ctx, smooth);
      ctx.stroke();
      ctx.restore();
      const last = coords[coords.length - 1];
      ctx.fillStyle = line.stroke;
      ctx.beginPath();
      ctx.arc(last.x, last.y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = theme.pointRing;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    function drawCrosshair(ctx, hover, scale) {
      const { pad, plotH } = scale;
      ctx.save();
      ctx.strokeStyle = theme.muted;
      ctx.setLineDash([4, 4]);
      ctx.globalAlpha = 0.7;
      ctx.beginPath();
      ctx.moveTo(hover.x, pad.top);
      ctx.lineTo(hover.x, pad.top + plotH);
      ctx.stroke();
      ctx.restore();
      for (const hit of hover.hits) {
        ctx.fillStyle = hit.stroke;
        ctx.beginPath();
        ctx.arc(hit.x, hit.y, 7, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = theme.pointRing;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    function nearestPointIndex(points, mouseX, scale) {
      let bestIndex = 0;
      let bestDistance = Infinity;
      for (let i = 0; i < points.length; i += 1) {
        const distance = Math.abs(pointX(i, scale) - mouseX);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestIndex = i;
        }
      }
      return bestIndex;
    }

    function renderLegend(lines) {
      const unit = config.unit;
      const items = lines.map(line => `
      <span class="legend-item">
        <span class="legend-swatch" style="background:${line.stroke}"></span>
        <strong>${escapeHtml(line.label)}</strong>
      </span>
    `).join("");
      const thresholdItem = config.threshold != null
        ? `<span class="legend-item"><span class="legend-swatch" style="background:${theme.threshold}"></span><strong>阈值 ${config.threshold}${unit}</strong></span>`
        : "";
      legendEl.innerHTML = items + thresholdItem;
    }

    function escapeHtml(text) {
      return String(text).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[ch]));
    }

    function draw() {
      const lines = config.lines;
      const ctx = fitCanvas();
      const { width, height } = canvas._logical;
      const scale = buildScale(lines);
      ctx.clearRect(0, 0, width, height);
      drawPlotBackground(ctx, scale);
      drawGridAndAxes(ctx, scale, lines);
      drawThreshold(ctx, scale);
      for (const line of lines) drawSeries(ctx, line, scale);
      if (hoverState) drawCrosshair(ctx, hoverState, scale);
      renderLegend(lines);
    }

    canvas.addEventListener("mousemove", event => {
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const lines = config.lines;
      if (!lines.length) return;
      const scale = buildScale(lines);
      const hits = [];
      const rows = [];
      let hoverTime = null;
      const unitSuffix = config.unit === "MB" ? " MB" : "%";
      for (const line of lines) {
        if (!line.points.length) continue;
        const pointIndex = nearestPointIndex(line.points, x, scale);
        const point = line.points[pointIndex];
        hoverTime = point.time;
        hits.push({
          x: pointX(pointIndex, scale),
          y: valueY(point.value, scale),
          stroke: line.stroke,
        });
        rows.push(`${line.label}: ${Number(point.value).toFixed(2)}${unitSuffix}`);
      }
      if (hoverTime) rows.unshift(formatTimeShort(hoverTime));
      hoverState = { x: hits[0]?.x || x, hits };
      tooltip.innerHTML = rows.map(r => `<div>${escapeHtml(r)}</div>`).join("");
      tooltip.classList.add("visible");
      tooltip.style.left = `${event.clientX - wrap.getBoundingClientRect().left}px`;
      tooltip.style.top = `${event.clientY - wrap.getBoundingClientRect().top - 12}px`;
      draw();
    });

    canvas.addEventListener("mouseleave", () => {
      hoverState = null;
      tooltip.classList.remove("visible");
      draw();
    });

    window.addEventListener("resize", draw);
    draw();
  }

  const configs = Array.isArray(window.CHART_CONFIGS)
    ? window.CHART_CONFIGS
    : (window.CHART_CONFIG ? [window.CHART_CONFIG] : []);
  for (const config of configs) mountChart(config);
})();
