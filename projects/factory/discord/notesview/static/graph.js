// The notes graph (HOME-920): a small force layout drawn as SVG. It reads
// the nodes and edges from the page's JSON data block, builds every element
// with createElementNS and textContent, and makes no network request.
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var box = document.getElementById("graph");
  var data = JSON.parse(document.getElementById("graph-data").textContent);
  var nodes = data.nodes,
    edges = data.edges;
  if (!nodes.length) {
    box.textContent = "No notes yet.";
    return;
  }

  // Layout: start on a circle, then spring edges, repel every pair and pull
  // gently to the middle. O(n^2) per step is fine for a few thousand notes.
  var n = nodes.length,
    i,
    j,
    k;
  var x = new Float64Array(n),
    y = new Float64Array(n);
  for (i = 0; i < n; i++) {
    var a = (2 * Math.PI * i) / n,
      r = 40 * Math.sqrt(n);
    x[i] = r * Math.cos(a);
    y[i] = r * Math.sin(a);
  }
  var steps = n > 1000 ? 120 : 300;
  for (k = 0; k < steps; k++) {
    var t = 1 - k / steps,
      fx = new Float64Array(n),
      fy = new Float64Array(n);
    for (i = 0; i < n; i++) {
      for (j = i + 1; j < n; j++) {
        var dx = x[i] - x[j],
          dy = y[i] - y[j],
          d2 = dx * dx + dy * dy + 0.01;
        var f = 2000 / d2;
        fx[i] += dx * f;
        fy[i] += dy * f;
        fx[j] -= dx * f;
        fy[j] -= dy * f;
      }
      fx[i] -= x[i] * 0.01;
      fy[i] -= y[i] * 0.01;
    }
    for (var e = 0; e < edges.length; e++) {
      var s = edges[e][0],
        d = edges[e][1];
      var ex = x[d] - x[s],
        ey = y[d] - y[s],
        dist = Math.sqrt(ex * ex + ey * ey) + 0.01;
      var pull = (dist - 60) * 0.05;
      fx[s] += (ex / dist) * pull;
      fy[s] += (ey / dist) * pull;
      fx[d] -= (ex / dist) * pull;
      fy[d] -= (ey / dist) * pull;
    }
    for (i = 0; i < n; i++) {
      var m = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]),
        cap = 20 * t + 1;
      if (m > cap) {
        fx[i] *= cap / m;
        fy[i] *= cap / m;
      }
      x[i] += fx[i];
      y[i] += fy[i];
    }
  }

  var minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  for (i = 0; i < n; i++) {
    minX = Math.min(minX, x[i]);
    maxX = Math.max(maxX, x[i]);
    minY = Math.min(minY, y[i]);
    maxY = Math.max(maxY, y[i]);
  }
  var pad = 60;
  var view = {
    x: minX - pad,
    y: minY - pad,
    w: maxX - minX + 2 * pad,
    h: maxY - minY + 2 * pad,
  };

  var svg = document.createElementNS(NS, "svg");
  function setView() {
    svg.setAttribute("viewBox", [view.x, view.y, view.w, view.h].join(" "));
  }
  setView();
  var gEdges = document.createElementNS(NS, "g");
  gEdges.setAttribute("class", "edges");
  for (var q = 0; q < edges.length; q++) {
    var line = document.createElementNS(NS, "line");
    line.setAttribute("x1", x[edges[q][0]]);
    line.setAttribute("y1", y[edges[q][0]]);
    line.setAttribute("x2", x[edges[q][1]]);
    line.setAttribute("y2", y[edges[q][1]]);
    gEdges.appendChild(line);
  }
  svg.appendChild(gEdges);
  var gNodes = document.createElementNS(NS, "g");
  gNodes.setAttribute("class", "nodes");
  for (i = 0; i < n; i++) {
    var link = document.createElementNS(NS, "a");
    // Only the viewer's own note URLs (graph data from the server).
    if (
      typeof nodes[i].url === "string" &&
      nodes[i].url.charAt(0) === "/" &&
      nodes[i].url.charAt(1) !== "/"
    ) {
      link.setAttribute("href", nodes[i].url);
    }
    var dot = document.createElementNS(NS, "circle");
    dot.setAttribute("cx", x[i]);
    dot.setAttribute("cy", y[i]);
    dot.setAttribute("r", 4 + Math.min(8, Math.sqrt(nodes[i].links) * 2));
    var title = document.createElementNS(NS, "title");
    title.textContent = nodes[i].name;
    dot.appendChild(title);
    var label = document.createElementNS(NS, "text");
    label.setAttribute("x", x[i]);
    label.setAttribute("y", y[i] - 10);
    label.textContent = nodes[i].name;
    link.appendChild(dot);
    link.appendChild(label);
    gNodes.appendChild(link);
  }
  svg.appendChild(gNodes);
  box.appendChild(svg);

  // Zoom with the wheel, pan with a drag.
  svg.addEventListener(
    "wheel",
    function (ev) {
      ev.preventDefault();
      var rect = svg.getBoundingClientRect();
      var px = view.x + ((ev.clientX - rect.left) / rect.width) * view.w;
      var py = view.y + ((ev.clientY - rect.top) / rect.height) * view.h;
      var z = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
      view.x = px - (px - view.x) * z;
      view.y = py - (py - view.y) * z;
      view.w *= z;
      view.h *= z;
      setView();
    },
    { passive: false },
  );
  var drag = null;
  svg.addEventListener("pointerdown", function (ev) {
    if (ev.target.closest && ev.target.closest("a")) return;
    drag = { x: ev.clientX, y: ev.clientY };
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    var rect = svg.getBoundingClientRect();
    view.x -= ((ev.clientX - drag.x) / rect.width) * view.w;
    view.y -= ((ev.clientY - drag.y) / rect.height) * view.h;
    drag = { x: ev.clientX, y: ev.clientY };
    setView();
  });
  svg.addEventListener("pointerup", function () {
    drag = null;
  });
})();
