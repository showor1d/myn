/* Read-only receipt: data is always text, never HTML. */
window.usageReceipt = function (data, act = () => {}) {
  const node = (tag, cls, text) => {
    const n = document.createElement(tag); n.className = cls || "";
    if (text != null) n.textContent = String(text);
    return n;
  };
  const row = (label, value, cls = "") => {
    const n = node("div", "receipt-row " + cls);
    n.append(node("span", "", label), node("span", "", value));
    return n;
  };
  const root = node("div", "usage-receipt-layout");
  root.dataset.printKey = data.print_key;
  const metrics = node("div", "usage-metrics");
  const expense = data.kind === "expenses";
  const animatedRings = [];
  const animatedBars = [], animatedPies = [], animatedLines = [];
  let drawPie = null, seriesPlot = null;
  const pieRevealPath = fraction => {
    if (fraction <= 0) return "M40 40 Z";
    if (fraction >= 1) return "M40 4 A36 36 0 1 1 40 76 A36 36 0 1 1 40 4 Z";
    const angle = -Math.PI/2 + fraction*Math.PI*2;
    return `M40 40 L40 4 A36 36 0 ${fraction>.5?1:0} 1 ${40+36*Math.cos(angle)} ${40+36*Math.sin(angle)} Z`;
  };
  for (const [label, value, key, caption] of expense ? [
    [data.metric_label, data.cost, "cost_usd", data.selected_category || "All categories"]
  ] : [
    ["TOKEN", data.tokens, "tokens", "Tokens in this period"],
    ["Cost", data.cost, "cost_usd", "USD"]
  ]) {
    const card = node("section", "usage-metric");
    const header = node("div", "expense-metric-header");
    header.append(node("div", "usage-eyebrow", label));
    if (expense) {
      const back = node("button", "expense-back", "back");
      back.type = "button"; back.disabled = data.at_present;
      back.setAttribute("aria-label", "Return to current period");
      back.addEventListener("click", () => act("now"));
      header.append(back);
    }
    card.append(header, node("div", "usage-number", value));
    if (!expense) card.append(node("div", "usage-caption", caption));
    if (expense && data.chart_kind === "pie") {
      const chart = node("div", "expense-pie");
      const ns = "http://www.w3.org/2000/svg";
      drawPie = (slices = data.chart_slices || [], totalOverride = null) => {
        chart.textContent = "";
        animatedPies.length = 0;
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("class", "expense-trend");
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", data.selected_category ?
          `${data.selected_category} expense share chart` : "Daily expense category share chart");
        const total = totalOverride ?? slices.reduce((sum, s) => sum+Number(s.value), 0);
        const height = Math.max(88, slices.length*13);
        svg.setAttribute("viewBox", `0 0 300 ${height}`);
        svg.style.height = `${height}px`;
        const pieBody = document.createElementNS(ns, "g");
        pieBody.setAttribute("transform", `translate(110 ${height/2-40})`);
        svg.append(pieBody);
        const wedges = document.createElementNS(ns, "g");
        if (total > 0) {
          const id = "expense-pie-reveal-" + (window.expensePieSerial = (window.expensePieSerial || 0)+1);
          const defs = document.createElementNS(ns, "defs");
          const clip = document.createElementNS(ns, "clipPath");
          clip.id = id; clip.setAttribute("clipPathUnits", "userSpaceOnUse");
          const reveal = document.createElementNS(ns, "path");
          reveal.setAttribute("class", "expense-pie-reveal");
          reveal.setAttribute("d", pieRevealPath(1));
          clip.append(reveal); defs.append(clip); pieBody.append(defs);
          wedges.setAttribute("clip-path", `url(#${id})`);
          animatedPies.push(reveal);
        }
        pieBody.append(wedges);
        const callouts = [];
        let angle = -Math.PI/2;
        for (const slice of slices) {
          const portion = Number(slice.value)/total, end = angle+portion*Math.PI*2;
          const shape = document.createElementNS(ns, portion >= 1 ? "circle" : "path");
          if (portion >= 1) {
            shape.setAttribute("cx", "40"); shape.setAttribute("cy", "40"); shape.setAttribute("r", "35");
          } else shape.setAttribute("d", `M40 40 L${40+35*Math.cos(angle)} ${40+35*Math.sin(angle)} A35 35 0 ${portion>.5?1:0} 1 ${40+35*Math.cos(end)} ${40+35*Math.sin(end)} Z`);
          shape.setAttribute("fill", slice.color); shape.classList.add("expense-slice");
          const title = document.createElementNS(ns, "title");
          const cost = totalOverride == null ? slice.value :
            (data.chart_slices || []).find(s=>s.name===slice.name)?.value || 0;
          title.textContent = `${slice.name} · ${(portion*100).toFixed(1)}% · $${Number(cost).toFixed(2)} USD`;
          shape.append(title); wedges.append(shape);
          const middle = portion >= 1 ? 0 : (angle+end)/2;
          callouts.push({name:slice.name, color:slice.color, middle,
            side:Math.cos(middle)>=0 ? 1 : -1, y:height/2+35*Math.sin(middle)});
          angle = end;
        }
        for (const side of [-1, 1]) {
          const items = callouts.filter(item => item.side === side).sort((a,b) => a.y-b.y);
          // Keep sparse labels level with their slice; only move crowded labels.
          const positions = items.map(item => item.y);
          for (let i=0; i<positions.length; i++)
            positions[i] = Math.max(positions[i], i ? positions[i-1]+13 : 7);
          for (let i=positions.length-1; i>=0; i--)
            positions[i] = Math.min(positions[i], i<positions.length-1 ? positions[i+1]-13 : height-7);
          items.forEach((item, index) => {
            const y = positions[index];
            const endX = 150+55*side;
            const leader = document.createElementNS(ns, "polyline");
            leader.setAttribute("class", "expense-pie-leader");
            leader.setAttribute("points", `${150+35*Math.cos(item.middle)},${item.y} ${150+41*side},${item.y} ${150+49*side},${y} ${endX},${y}`);
            leader.style.stroke = item.color; svg.append(leader);
            const label = document.createElementNS(ns, "text");
            label.setAttribute("class", "expense-pie-label");
            label.setAttribute("x", endX+4*side); label.setAttribute("y", y);
            label.setAttribute("text-anchor", side>0 ? "start" : "end");
            label.setAttribute("dominant-baseline", "middle"); label.textContent = item.name;
            svg.append(label);
          });
        }
        if (!total) {
          const empty = document.createElementNS(ns, "circle");
          empty.setAttribute("cx", "40"); empty.setAttribute("cy", "40"); empty.setAttribute("r", "35");
          empty.setAttribute("fill", "#a395c72e"); pieBody.append(empty);
        }
        chart.append(svg);
      };
      drawPie();
      card.append(chart); metrics.append(card);
      continue;
    }
    const values = data.series[key] || [];
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 300 60");
    if (expense) {
      svg.setAttribute("class", "expense-trend");
      svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Expense trend in USD");
      svg.setAttribute("preserveAspectRatio", "none");
      const baseline = document.createElementNS(ns, "line");
      for (const [k,v] of Object.entries({x1:4,y1:56,x2:296,y2:56,stroke:"#bee8f126"})) baseline.setAttribute(k,v);
      svg.append(baseline);
    } else svg.setAttribute("aria-hidden", "true");
    const line = document.createElementNS(ns, "polyline");
    const max = Math.max(1, ...values);
    const coords = values.map((v, i) =>
      [4 + (expense && data.series_positions ? data.series_positions[i] : i / Math.max(1, values.length-1)) * 292,
       56 - Math.max(0, v) / max * 48]);
    line.setAttribute("points", coords.map(p => p.join(",")).join(" "));
    const bars = expense && data.chart_kind === "bar";
    let plot = svg;
    if (expense && !bars) {
      const id = "expense-line-reveal-" + (window.expenseLineSerial = (window.expenseLineSerial || 0)+1);
      const clip = document.createElementNS(ns, "clipPath");
      clip.id = id; clip.setAttribute("clipPathUnits", "userSpaceOnUse");
      const reveal = document.createElementNS(ns, "rect");
      reveal.setAttribute("class", "expense-line-reveal");
      reveal.setAttribute("x", "0"); reveal.setAttribute("y", "0");
      reveal.setAttribute("width", "300"); reveal.setAttribute("height", "60");
      clip.append(reveal); svg.append(clip);
      plot = document.createElementNS(ns, "g");
      plot.setAttribute("clip-path", `url(#${id})`); svg.append(plot);
      animatedLines.push(reveal);
    }
    if (!bars) plot.appendChild(line);
    if (expense) seriesPlot = {line, coords, dots:[], bars};
    card.appendChild(svg); metrics.appendChild(card);
    if (expense) {
      coords.forEach(([x,y], i) => {
        const dot = document.createElementNS(ns, bars ? "rect" : "circle");
        if (bars) {
          const width = Math.min(32, 180/Math.max(1, values.length));
          dot.classList.add("expense-bar");
          dot.setAttribute("x", x-width/2); dot.setAttribute("y", y);
          dot.setAttribute("width", width); dot.setAttribute("height", 56-y);
          dot.setAttribute("rx", "2");
          animatedBars.push([dot, 56-y]);
        } else {
          dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("r", "2");
        }
        dot.setAttribute("fill", "#b594f0");
        const title = document.createElementNS(ns, "title");
        title.textContent = `${data.series_labels?.[i] || ""} · $${Number(values[i]).toFixed(2)} USD`;
        dot.append(title); plot.append(dot);
        seriesPlot.dots.push(dot);
      });
      const axis = node("div", "expense-trend-labels");
      const ticks = data.trend_ticks || [];
      if (ticks.length > 7) axis.classList.add("dense");
      for (const [position, label] of ticks) {
        const tick = node("span", "", label);
        tick.style.left = `${position*100}%`;
        tick.style.transform = position === 0 ? "none" : position === 1 ? "translateX(-100%)" : "translateX(-50%)";
        axis.append(tick);
      }
      card.append(axis);
    }
  }
  if (expense) {
    root.classList.add("expense-layout");
    const category = node("section", "expense-categories");
    const header = node("div", "expense-category-header");
    header.appendChild(node("div", "usage-eyebrow", "CATEGORY"));
    category.appendChild(header);
    const list = node("div", "expense-category-list");
    list.setAttribute("aria-label", "Expense categories"); list.tabIndex = 0;
    for (const item of data.categories) {
      const b = node("button", "expense-category" + (data.selected_category === item.name ? " selected" : ""));
      b.type = "button";
      b.setAttribute("aria-pressed", String(data.selected_category === item.name));
      const ring = node("span", "category-ring");
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 48 48"); svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("shape-rendering", "geometricPrecision");
      for (const [cls, radius, width, color] of [
        ["category-ring-track",19.5,7.5,"#a395c72e"],
        ["category-ring-progress",19.5,7.5,"#9744ec"]
      ]) {
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("class", cls);
        circle.setAttribute("cx", "24"); circle.setAttribute("cy", "24");
        circle.setAttribute("r", radius); circle.setAttribute("pathLength", "100");
        circle.setAttribute("transform", "rotate(-90 24 24)");
        circle.setAttribute("stroke", color); circle.setAttribute("stroke-width", width);
        if (cls === "category-ring-progress") {
          const percent = Math.min(100, Math.max(0, Number(item.percent) || 0));
          circle.setAttribute("stroke-dasharray", `${percent} 100`);
          if (!percent) circle.style.display = "none";
          animatedRings.push([circle, percent, item.name]);
        }
        svg.appendChild(circle);
      }
      ring.append(svg);
      b.append(ring, node("span", "", item.name));
      b.title = item.cost; b.addEventListener("click", () => act("category:" + item.name)); list.appendChild(b);
    }
    category.appendChild(list); metrics.appendChild(category);
  }
  const printer = node("section", "receipt-printer");
  const slot = node("div", "receipt-slot"); slot.setAttribute("aria-hidden", "true");
  const stub = node("div", "receipt-stub"); stub.setAttribute("aria-hidden", "true");
  const feed = node("div", "receipt-feed");
  const paper = node("article", "usage-paper"); paper.setAttribute("aria-label", "Usage receipt");
  // The torn strip occupies only the first 10px; printed content starts at 28px.
  const torn = [[0,5],[3,7],[5,3],[8,6],[11,4],[13,8],[16,5],[19,6],[22,2],
    [25,5],[28,9],[30,5],[34,7],[37,3],[40,6],[43,4],[46,8],[49,5],[52,7],
    [56,2],[59,5],[62,4],[65,8],[68,6],[71,9],[74,4],[77,6],[80,3],[84,7],
    [87,5],[90,8],[93,4],[96,6],[98,3],[100,5]];
  const outline = torn.map(([x,y]) => `${x}% ${y}px`);
  for (let x=100; x>=0; x-=2) outline.push(`${x}% ${x%4 ? "100%" : "calc(100% - 5px)"}`);
  paper.style.setProperty("--receipt-torn-edge", `polygon(${outline.join(",")})`);
  // Both sides of a tear share the same contour, translated only vertically.
  stub.style.clipPath = `polygon(0 0,100% 0,${[...torn].reverse().map(([x,y]) => `${x}% ${y}px`).join(",")})`;
  const tearEdge = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  tearEdge.setAttribute("class", "receipt-tear-edge");
  tearEdge.setAttribute("viewBox", "0 0 1000 12");
  tearEdge.setAttribute("preserveAspectRatio", "none");
  tearEdge.setAttribute("aria-hidden", "true");
  const edgeLine = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  edgeLine.setAttribute("points", torn.map(([x,y]) => `${x*10},${y+1}`).join(" "));
  tearEdge.appendChild(edgeLine);
  const cutLight = edgeLine.cloneNode();
  cutLight.setAttribute("class", "receipt-tear-light");
  cutLight.setAttribute("pathLength", "100");
  const cutOutline = cutLight.cloneNode();
  cutOutline.setAttribute("class", "receipt-tear-light receipt-tear-light-outline");
  tearEdge.appendChild(cutOutline);
  tearEdge.appendChild(cutLight);
  paper.appendChild(tearEdge);
  // Paper ahead of the cutting tip still bridges the mouth and the lower sheet.
  // This is real paper geometry, not a dark line painted across an already open gap.
  const join = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  join.setAttribute("class", "receipt-join"); join.setAttribute("aria-hidden", "true");
  join.setAttribute("preserveAspectRatio", "none"); join.style.display = "none";
  const connection = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
  join.appendChild(connection); printer.appendChild(join);
  let tearFrame = null;
  root.stopPrint = () => {
    if (tearFrame !== null) cancelAnimationFrame(tearFrame);
    tearFrame = null; join.style.display = "none"; tearEdge.style.clipPath = "";
  };
  root.paintTear = () => {
    // Window resizing must keep the remaining paper the same width as the receipt.
    stub.style.width = paper.offsetWidth + "px";
    const light = getComputedStyle(cutLight);
    const progress = Math.max(0, Math.min(1, (10-parseFloat(light.strokeDashoffset))/100));
    const point = edgeLine.getPointAtLength(edgeLine.getTotalLength()*progress);
    const percent = point.x/10;
    if (!root.classList.contains("printing") || progress >= 1 || Number(light.opacity) === 0) {
      join.style.display = "none"; tearEdge.style.clipPath = ""; return;
    }
    tearEdge.style.clipPath = `inset(0 ${100-percent}% 0 0)`;
    const mouth = stub.getBoundingClientRect(), sheet = paper.getBoundingClientRect();
    const host = printer.getBoundingClientRect(), gap = Math.max(0, sheet.top-mouth.top);
    const remaining = [[point.x,point.y-1], ...torn.filter(([x]) => x*10>point.x).map(([x,y]) => [x*10,y])];
    const polygon = [...remaining.map(([x,y]) => [x,y-.5]),
      ...[...remaining].reverse().map(([x,y]) => [x,y+gap+.5])];
    connection.setAttribute("points", polygon.map(p => p.join(",")).join(" "));
    join.setAttribute("viewBox", `0 0 1000 ${gap+12}`);
    Object.assign(join.style, {display:"block", left:(sheet.left-host.left)+"px",
      top:(mouth.top-host.top)+"px", width:sheet.width+"px", height:(gap+12)+"px"});
    join.dataset.cut = String(percent);
  };
  paper.tabIndex = 0;
  paper.setAttribute("role", "button");
  paper.setAttribute("aria-label", expense ? "View full expense receipt" : "View full usage receipt");
  const tokenCategory = expense && data.selected_category === "token";
  const subtitle = node("div", "receipt-subtitle", data.heading || "USAGE RECEIPT");
  if (tokenCategory) subtitle.appendChild(node("div", "receipt-heading-count",
    Number(data.token_total || 0).toLocaleString("en-US")));
  paper.append(node("div", "receipt-brand", "MYN"),
    subtitle,
    node("div", "receipt-date", data.date), node("div", "receipt-rule"));
  paper.append(row(expense ? "Item" : "Source / TOKEN", expense ? data.currency : "USD", "receipt-column-head"));
  if (expense && data.zero) {
    const smile = node("img", "receipt-smile");
    smile.alt = "Zero-spending character illustration"; smile.src = data.zero_art;
    paper.append(smile, node("p", "receipt-empty", "No spending this period"),
      node("p", "receipt-zero-message", "Nicely done!"));
  } else if (!data.rows.length) paper.append(node("p", "receipt-empty", "No usage records this period"));
  let usageCategory = null;
  for (const item of data.rows) {
    if (item.usage_category && item.usage_category !== usageCategory) {
      usageCategory = item.usage_category;
      paper.appendChild(node("div", "receipt-use-category", usageCategory));
    }
    const entry = node("div", "receipt-entry");
    entry.append(row(item.name, item.cost), node("div", "receipt-token", item.detail || (expense ? "" : item.tokens + " Token")));
    paper.appendChild(entry);
  }
  paper.append(node("div", "receipt-rule"), row("TOTAL", data.cost, "receipt-total"));
  if (expense) {
    for (const [code, value] of data.totals) if (code !== "USD") paper.appendChild(row(code, value));
    if (data.token_total && !tokenCategory) paper.appendChild(row("Token", Number(data.token_total).toLocaleString("en-US")));
    paper.appendChild(row("Line items", data.count));
  }
  else {
    paper.appendChild(row("Token", data.tokens));
    for (const [label, value] of data.kinds) paper.appendChild(row(label, value, "receipt-small"));
    paper.append(node("div", "receipt-rule"), row("Calls", data.calls), row("Web searches", data.searches));
  }
  if (data.unpriced) paper.appendChild(node("p", "receipt-footnote", data.unpriced));
  paper.append(node("p", "receipt-footnote", data.note), node("div", "receipt-end", "—  THANK YOU  —"));
  feed.appendChild(paper); printer.append(slot, stub, feed);
  const comparison = node("div", "receipt-comparison " + data.trend);
  comparison.append(node("strong", "", data.change), node("span", "", data.comparison + "Cost"));
  root.append(metrics, printer);
  if (!expense) root.appendChild(comparison);
  root.dataset.phase = "detached";
  let metricsFrame = null;
  const amount = metrics.querySelector(".usage-number");
  const total = Number(String(data.cost).replace(/[^0-9.-]/g, ""));
  const pieTotal = (data.chart_slices || []).reduce((sum, s) => sum+Number(s.value), 0);
  const target = {
    amount: total,
    rings: Object.fromEntries(animatedRings.map(([,value,name]) => [name,value])),
    pies: (data.chart_slices || []).map(s => ({...s, value:Number(s.value)/pieTotal})),
    series: Object.fromEntries((seriesPlot?.coords || []).map(([x,y],i) =>
      [data.series_labels[i], [x,56-y]])),
    reveal:1
  };
  root.metricsTarget = JSON.stringify(target);
  let current = null, transitionFrom = null;
  root.metricsSnapshot = () => current && structuredClone(current);
  const mix = (a,b,p) => a+(b-a)*p;
  const paintMetrics = (fraction, ringFraction) => {
    if (!expense) return;
    if (transitionFrom) {
      const from = transitionFrom;
      const pies = [...new Set([...from.pies.map(s=>s.name), ...target.pies.map(s=>s.name)])]
        .map(name => {
          const a=from.pies.find(s=>s.name===name), b=target.pies.find(s=>s.name===name);
          return {...(b || a), value:mix(a?.value || 0,b?.value || 0,ringFraction)};
        }).filter(s=>s.value>0);
      current = {amount:mix(from.amount,total,fraction), pies,
        rings:Object.fromEntries(Object.entries(target.rings).map(([name,v]) =>
          [name,mix(from.rings[name] || 0,v,ringFraction)])),
        series:Object.fromEntries(Object.entries(target.series).map(([name,[x,h]]) =>
          [name,[mix(from.series[name]?.[0] ?? x,x,ringFraction),
                 mix(from.series[name]?.[1] || 0,h,ringFraction)]])),
        reveal:mix(from.reveal,1,ringFraction)};
      amount.textContent = "$" + current.amount.toLocaleString("en-US", {
        minimumFractionDigits:2, maximumFractionDigits:2}) + " USD";
      for (const [circle,,name] of animatedRings) {
        const value = current.rings[name];
        circle.setAttribute("stroke-dasharray", `${value} 100`);
        circle.style.display = value>0 ? "" : "none";
      }
      if (drawPie) drawPie(current.pies, current.pies.length ? 1 : 0);
      if (seriesPlot) {
        const points = data.series_labels.map(name => current.series[name]);
        seriesPlot.line.setAttribute("points", points.map(([x,h])=>`${x},${56-h}`).join(" "));
        seriesPlot.dots.forEach((dot,i) => {
          const [x,h] = points[i];
          if (seriesPlot.bars) {
            dot.setAttribute("x",x-Number(dot.getAttribute("width"))/2);
            dot.setAttribute("y",56-h); dot.setAttribute("height",h);
          } else {
            dot.setAttribute("cx",x); dot.setAttribute("cy",56-h);
          }
        });
        for (const reveal of animatedLines) reveal.setAttribute("width",300*current.reveal);
      }
      return;
    }
    let remaining = ringFraction;
    current = {amount:total*fraction, rings:{}, series:{}, reveal:ringFraction,
      pies:target.pies.map(s => {
        const value=Math.min(s.value,Math.max(0,remaining)); remaining-=s.value;
        return {...s,value};
      })};
    amount.textContent = fraction >= 1 ? data.cost :
      "$" + (total * fraction).toLocaleString("en-US", {
        minimumFractionDigits:2, maximumFractionDigits:2
      }) + " USD";
    for (const [circle, target, name] of animatedRings) {
      const value = target * ringFraction;
      current.rings[name] = value;
      circle.setAttribute("stroke-dasharray", `${value} 100`);
      circle.style.display = value > 0 ? "" : "none";
    }
    for (const [bar, height] of animatedBars) {
      bar.setAttribute("height", height*ringFraction);
      bar.setAttribute("y", 56-height*ringFraction);
    }
    for (const reveal of animatedPies) reveal.setAttribute("d", pieRevealPath(ringFraction));
    for (const reveal of animatedLines) reveal.setAttribute("width", 300*ringFraction);
    for (const [name,[x,h]] of Object.entries(target.series))
      current.series[name] = [x,h*(seriesPlot?.bars ? ringFraction : 1)];
  };
  root.stopMetrics = () => {
    if (metricsFrame !== null) cancelAnimationFrame(metricsFrame);
    metricsFrame = null;
  };
  root.startMetrics = (started = performance.now(), from = null) => {
    if (!expense) return;
    transitionFrom = from;
    root.metricsFrom = from;
    root.stopMetrics();
    root.metricsStartedAt = started;
    const tick = now => {
      metricsFrame = null;
      if (!root.isConnected) return;
      const progress = matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 :
        Math.min(1, Math.max(0, (now-started)/1200));
      paintMetrics(1-Math.pow(1-progress, 3), progress);
      if (progress < 1) metricsFrame = requestAnimationFrame(tick);
    };
    tick(performance.now());
  };
  root.startPrint = (started = performance.now(), animateMetrics = true) => {
    root.stopPrint();
    root.printStartedAt = started;
    if (animateMetrics) root.startMetrics();
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const elapsed = Math.max(0, performance.now() - started);
    root.style.setProperty("--receipt-delay", -elapsed + "ms");
    root.dataset.phase = elapsed < 2000 ? "printing" : "detached";
    root.classList.toggle("printing", elapsed < 2000);
    requestAnimationFrame(() => {
      if (root.isConnected) {
        stub.style.right = "auto";
        stub.style.width = paper.offsetWidth + "px";
      }
    });
    stub.classList.toggle("receipt-stub-anim", elapsed < 3100);
    const detached = event => {
      if (event.target !== paper) return;
      root.dataset.phase = "detached";
      root.classList.remove("printing");
      paper.removeEventListener("animationend", detached);
      paper.removeEventListener("animationcancel", detached);
    };
    if (elapsed < 2000) {
      paper.addEventListener("animationend", detached);
      paper.addEventListener("animationcancel", detached);
      const tickTear = () => {
        tearFrame = null;
        if (!root.isConnected || !root.classList.contains("printing")) { root.stopPrint(); return; }
        root.paintTear(); tearFrame = requestAnimationFrame(tickTear);
      };
      tearFrame = requestAnimationFrame(tickTear);
    }
  };
  let press = null;
  paper.addEventListener("pointerdown", e => { press = [e.clientX, e.clientY, feed.scrollTop]; });
  paper.addEventListener("click", e => {
    if (root.dataset.phase !== "detached") return;
    if (press && (Math.hypot(e.clientX-press[0], e.clientY-press[1]) > 7 ||
                  Math.abs(feed.scrollTop-press[2]) > 7)) { press = null; return; }
    press = null;
    window.ReceiptViewer.open(paper, data.date);
  });
  paper.addEventListener("keydown", e => {
    if ((e.key === "Enter" || e.key === " ") && root.dataset.phase === "detached") {
      e.preventDefault(); window.ReceiptViewer.open(paper, data.date);
    }
  });
  return root;
};

/* A local SVG image of the entire receipt; no upload, canvas limit or external asset. */
window.ReceiptViewer = (() => {
  let overlay = null, url = null, origin = null;
  function close() {
    overlay?.remove(); overlay = null;
    if (url) URL.revokeObjectURL(url);
    url = null;
    if (origin?.isConnected) origin.focus({preventScroll:true});
    origin = null;
  }
  function open(paper, date) {
    close(); origin = paper;
    const clone = paper.cloneNode(true);
    const originals = [paper, ...paper.querySelectorAll("*")];
    const copies = [clone, ...clone.querySelectorAll("*")];
    originals.forEach((item, i) => {
      const style = getComputedStyle(item);
      for (const prop of style) copies[i].style.setProperty(prop, style.getPropertyValue(prop));
      copies[i].removeAttribute("tabindex");
    });
    const width = Math.ceil(paper.getBoundingClientRect().width), height = paper.scrollHeight;
    clone.style.cssText += `;margin:0;box-sizing:border-box;width:${width}px;height:${height}px;animation:none;transform:none;`;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><foreignObject width="100%" height="100%">${new XMLSerializer().serializeToString(clone)}</foreignObject></svg>`;
    url = URL.createObjectURL(new Blob([svg], {type:"image/svg+xml"}));
    overlay = document.createElement("section"); overlay.className = "receipt-viewer";
    overlay.setAttribute("role", "dialog"); overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Full receipt");
    const toolbar = document.createElement("div"); toolbar.className = "receipt-viewer-bar";
    const title = document.createElement("span"); title.textContent = "Receipt · " + date; toolbar.appendChild(title);
    const stage = document.createElement("div"); stage.className = "receipt-image-stage fit";
    const img = document.createElement("img"); img.src = url; img.alt = "Full usage receipt";
    img.alt = "Full receipt";
    img.draggable = false; stage.appendChild(img);
    let scale = 1;
    const zoom = value => {
      scale = Math.max(.25, Math.min(4, value));
      stage.classList.remove("fit"); img.style.width = `${width * scale}px`;
    };
    function button(label, action) {
      const b = document.createElement("button"); b.textContent = label; b.type = "button";
      b.addEventListener("click", action); toolbar.appendChild(b); return b;
    }
    button("−", () => zoom(scale / 1.25));
    button("+", () => zoom(scale * 1.25));
    button("Fit receipt", () => { stage.classList.add("fit"); img.style.width = ""; scale = 1; });
    button("Original size", () => zoom(1));
    const exit = button("Close", close);
    overlay.append(toolbar, stage); document.body.appendChild(overlay); exit.focus();
    overlay.addEventListener("click", e => { if (e.target === stage) close(); });
  }
  document.addEventListener("keydown", e => {
    if (!overlay) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(); }
    if (e.key === "Tab") {
      const buttons = [...overlay.querySelectorAll("button")], at = buttons.indexOf(document.activeElement);
      e.preventDefault(); buttons[(at + (e.shiftKey ? buttons.length-1 : 1)) % buttons.length].focus();
    }
  }, true);
  return {open, close};
})();
