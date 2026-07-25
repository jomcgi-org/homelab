<script>
  import rough from "roughjs";
  import { marked } from "marked";
  import { createGraphState } from "./graph-layout.js";

  /** Strip YAML frontmatter and the auto-generated LINKS section from note markdown. */
  function cleanNoteContent(md) {
    if (!md) return "";
    // Remove YAML frontmatter (---\n...\n---)
    let cleaned = md.replace(/^---\n[\s\S]*?\n---\n?/, "");
    // Remove trailing Links section (any heading level or bare line, case-insensitive)
    cleaned = cleaned.replace(/\n#{0,3}\s*Links\s*\n[\s\S]*$/i, "");
    return cleaned.trim();
  }

  let messages = $state([]);
  let inputText = $state("");
  let isStreaming = $state(false);
  let chatLog;
  let hoveredNode = $state(null);
  let selectedNode = $state(null);
  let drawerNote = $state(null);
  let drawerLoading = $state(false);

  const graphState = createGraphState();
  let layoutResult = $state({ nodes: [], edges: [], nodeMap: {} });
  let svgEl = $state(null);

  const TYPE_COLORS = {
    note: { border: "#ffd54f" },
    atom: { border: "#64a0ff" },
    fact: { border: "#00c853" },
    paper: { border: "#2979ff" },
    article: { border: "#ff6d00" },
    recipe: { border: "#d500f9" },
  };

  const EDGE_COLORS = {
    link: "#bbb",
    related: "#ffd54f",
    derives_from: "#1a1a1a",
    edge: "#bbb",
  };

  /** Remove all child nodes from an element. */
  function clearChildren(el) {
    if (!el) return;
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function scrollToBottom() {
    if (chatLog) {
      requestAnimationFrame(() => {
        chatLog.scrollTop = chatLog.scrollHeight;
      });
    }
  }

  async function sendMessage() {
    const text = inputText.trim();
    if (!text || isStreaming) return;

    inputText = "";
    messages.push({ role: "user", content: text });
    messages.push({ role: "assistant", content: "" });
    isStreaming = true;
    scrollToBottom();

    try {
      const res = await fetch("/private/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          history: messages.slice(0, -2).map((m) => ({
            role: m.role,
            content: m.content,
          })),
        }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const event = JSON.parse(line.slice(6));

            if (event.type === "text_chunk") {
              messages[messages.length - 1].content += event.data.text;
              scrollToBottom();
            } else if (event.type === "error") {
              messages[messages.length - 1].content +=
                `\n[Error: ${event.data.message}]`;
            } else if (event.type === "done") {
              // Stream complete
            } else {
              // Graph events: node_discovered, node_discarded, edge_traversed
              enqueueGraphEvent(event);
            }
          } catch {
            // Skip malformed events
          }
        }
      }
    } catch (err) {
      messages[messages.length - 1].content +=
        `\n[Connection error: ${err.message}]`;
    } finally {
      isStreaming = false;
    }
  }

  function onKeydown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  function onGlobalKeydown(e) {
    if (e.key === "Escape" && selectedNode) {
      selectedNode = null;
    }
  }

  // Fetch note content when a node is selected
  $effect(() => {
    if (!selectedNode) {
      drawerNote = null;
      return;
    }
    drawerLoading = true;
    fetch(`/private/chat?note_id=${encodeURIComponent(selectedNode)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((note) => {
        drawerNote = note;
        drawerLoading = false;
      })
      .catch(() => {
        drawerNote = null;
        drawerLoading = false;
      });
  });

  // Animated graph event queue — pencil→ink drawing for new nodes
  //
  // Flow per cycle:
  //   1. Absorb all queued events into graph state at once
  //   2. Re-layout → existing nodes CSS-transition to new positions (MOVE_MS)
  //   3. After slide settles, draw each new node: pencil sketch → ink retrace → text jot
  //   4. Drain again (absorbs anything that arrived during animation)
  const MOVE_MS = 400;
  const PEN_EASE = "cubic-bezier(0.65, 0, 0.15, 1)";
  const PENCIL_MS = 600; // pencil sketch total (4 sides)
  const INK_MS = 500; // ink retrace total (4 sides)
  const TEXT_MS = 300; // text fade-in
  const EDGE_PENCIL_MS = 400; // pencil line between nodes
  const EDGE_INK_MS = 340; // ink retrace on edge
  let eventQueue = [];
  let draining = false;
  let pendingRevealIds = new Set();
  let pendingEdgeKeys = new Set();

  function enqueueGraphEvent(evt) {
    eventQueue.push(evt);
    if (!draining) drainQueue();
  }

  function drainQueue() {
    if (eventQueue.length === 0) {
      draining = false;
      return;
    }
    draining = true;

    // Absorb ALL queued events at once into graph state
    const newNodeIds = [];
    while (eventQueue.length > 0) {
      const evt = eventQueue.shift();
      if (evt.type === "node_discovered") {
        newNodeIds.push(evt.data.note_id);
        pendingRevealIds.add(evt.data.note_id);
        graphState.addNode(evt.data);
        for (const edge of evt.data.edges || []) {
          graphState.addEdge(
            evt.data.note_id,
            edge.target_id,
            edge.edge_type || "link",
          );
        }
      } else if (evt.type === "edge_traversed") {
        graphState.addEdge(
          evt.data.from_id,
          evt.data.to_id,
          evt.data.edge_type,
        );
      } else if (evt.type === "node_discarded") {
        graphState.discardNode(evt.data.note_id);
      }
    }

    // Re-layout — existing nodes slide via CSS transition, new nodes drawn hidden
    layoutResult = graphState.layout();

    if (newNodeIds.length === 0) {
      // No new nodes, just edge/discard events — settle quickly
      setTimeout(drainQueue, 60);
      return;
    }

    // After existing nodes finish sliding, sequentially reveal each new node
    setTimeout(() => revealNodes(newNodeIds, 0), MOVE_MS);
  }

  function revealNodes(ids, idx) {
    if (idx >= ids.length) {
      // All revealed — drain again to pick up anything that queued during animation
      drainQueue();
      return;
    }
    const id = ids[idx];
    pendingRevealIds.delete(id);
    const entry = drawnNodes.get(id);
    if (!entry) {
      revealNodes(ids, idx + 1);
      return;
    }

    // Animate the node box: pencil → ink → fill → text
    animateNodeEntry(entry);

    // Animate any pending edges connected to this node (after node ink starts)
    setTimeout(
      () => {
        for (const [key, edgeEntry] of drawnEdges) {
          if (
            pendingEdgeKeys.has(key) &&
            (edgeEntry.from === id || edgeEntry.to === id)
          ) {
            pendingEdgeKeys.delete(key);
            animateEdgeEntry(edgeEntry);
          }
        }
      },
      PENCIL_MS + INK_MS * 0.3,
    ); // start edge draw as node ink is partway through

    // Total animation time for one node + its edges, then start next
    const nodeAnimMs =
      PENCIL_MS +
      INK_MS +
      Math.max(TEXT_MS, EDGE_PENCIL_MS + EDGE_INK_MS) * 0.6;
    setTimeout(() => revealNodes(ids, idx + 1), nodeAnimMs);
  }

  /** Animate a node's pencil sketch → ink retrace → fill → text appearance. */
  function animateNodeEntry(entry) {
    const { g } = entry;

    // Show the group container (was opacity 0)
    g.style.transition = "none";
    g.style.opacity = "1";
    g.style.transform = `translate(${entry.origX}px, ${entry.origY}px) scale(1)`;

    // Collect pencil elements (one per side, each may have 1-2 paths)
    const pencilEls = g.querySelectorAll("[data-layer='pencil']");
    const inkEls = g.querySelectorAll("[data-layer='ink']");
    const numSides = Math.max(pencilEls.length, 1);
    const pencilPerSide = PENCIL_MS / numSides;
    const inkPerSide = INK_MS / numSides;

    // Phase 1: Pencil sketch — light gray lines draw in sequentially
    pencilEls.forEach((el, sideIdx) => {
      el.style.opacity = "0.5"; // make visible (was 0 for pending)
      const delay = sideIdx * pencilPerSide;
      el.querySelectorAll("path").forEach((path) => {
        try {
          const len = path.getTotalLength();
          path.style.strokeDasharray = String(len);
          path.style.strokeDashoffset = String(len);
          path.style.animation = `edgeDraw ${pencilPerSide}ms ${PEN_EASE} ${delay}ms forwards`;
        } catch {
          path.style.opacity = "0";
          path.style.animation = `nodeIn 100ms ease ${delay}ms forwards`;
        }
      });
    });

    // Phase 2: Ink retrace — colored border draws over pencil, starts after pencil completes
    inkEls.forEach((el, sideIdx) => {
      const delay = PENCIL_MS + sideIdx * inkPerSide;
      el.querySelectorAll("path").forEach((path) => {
        // strokeDasharray/offset was set in drawNode for pending nodes
        path.style.animation = `edgeDraw ${inkPerSide}ms ${PEN_EASE} ${delay}ms forwards`;
      });
    });

    // Phase 3: Fill scrubs in after ink outline completes
    const fillEl = g.querySelector("[data-layer='fill']");
    if (fillEl) {
      fillEl.style.animation = `nodeIn ${TEXT_MS}ms ease-out ${PENCIL_MS + INK_MS}ms forwards`;
    }

    // Phase 4: Text jots in (overlaps with ink tail end)
    const textEl = g.querySelector("text");
    if (textEl) {
      textEl.style.animation = `textJot ${TEXT_MS}ms ease ${PENCIL_MS + INK_MS * 0.6}ms forwards`;
    }

    // Re-enable CSS transition for future position changes
    const totalMs = PENCIL_MS + INK_MS + TEXT_MS;
    setTimeout(() => {
      g.style.transition = `transform ${MOVE_MS}ms ease, opacity 0.25s ease`;
    }, totalMs);
  }

  /** Return the set of node IDs connected to focusId (including itself). */
  function getConnectedNodes(focusId) {
    if (!focusId) return null;
    const connected = new Set([focusId]);
    for (const edge of layoutResult.edges) {
      if (edge.from === focusId) connected.add(edge.to);
      if (edge.to === focusId) connected.add(edge.from);
    }
    return connected;
  }

  const NODE_H = 44;

  // Persistent refs — survive across effect runs so we can diff
  let drawnNodes = new Map(); // id → { g, origX, origY }
  let drawnEdges = new Map(); // "from|to" → { el, from, to }
  let nodeElsForDim = new Map();
  let edgeElsForDim = new Map();

  /** Deterministic seed from string. */
  function seed(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++)
      h = ((h << 5) - h + str.charCodeAt(i)) | 0;
    return Math.abs(h);
  }

  /** Draw pencil + ink rough.js lines inside an edge group. */
  function fillEdgeGroup(rc, g, x1, y1, x2, y2, color, edgeSeed, isPending) {
    // Pencil: light gray sketch
    const pencil = rc.line(x1, y1, x2, y2, {
      stroke: "#c0b8a8",
      roughness: 1,
      strokeWidth: 1,
      seed: edgeSeed,
    });
    pencil.dataset.layer = "pencil";
    pencil.style.opacity = isPending ? "0" : "0.4";
    g.appendChild(pencil);

    // Ink: colored retrace
    const ink = rc.line(x1, y1, x2, y2, {
      stroke: color,
      roughness: 1,
      strokeWidth: 1.2,
      seed: edgeSeed + 7,
    });
    ink.dataset.layer = "ink";
    if (isPending) {
      ink.querySelectorAll("path").forEach((p) => {
        try {
          const len = p.getTotalLength();
          p.style.strokeDasharray = String(len);
          p.style.strokeDashoffset = String(len);
        } catch {
          /* fallback handled in animation */
        }
      });
    } else {
      ink.style.opacity = "0.5";
    }
    g.appendChild(ink);
  }

  /** Animate a new edge's pencil→ink draw-in. */
  function animateEdgeEntry(edgeEntry) {
    const { el: g } = edgeEntry;
    const pencilEl = g.querySelector("[data-layer='pencil']");
    const inkEl = g.querySelector("[data-layer='ink']");

    // Pencil sketch draws in
    if (pencilEl) {
      pencilEl.style.opacity = "0.4";
      pencilEl.querySelectorAll("path").forEach((path) => {
        try {
          const len = path.getTotalLength();
          path.style.strokeDasharray = String(len);
          path.style.strokeDashoffset = String(len);
          path.style.animation = `edgeDraw ${EDGE_PENCIL_MS}ms ${PEN_EASE} 0ms forwards`;
        } catch {
          path.style.opacity = "0";
          path.style.animation = `nodeIn 150ms ease forwards`;
        }
      });
    }

    // Ink chases after pencil
    if (inkEl) {
      inkEl.style.opacity = "0.5";
      inkEl.querySelectorAll("path").forEach((path) => {
        path.style.animation = `edgeDraw ${EDGE_INK_MS}ms ${PEN_EASE} ${EDGE_PENCIL_MS}ms forwards`;
      });
    }
  }

  function drawNode(rc, node) {
    const colors = TYPE_COLORS[node.type] || TYPE_COLORS.note;
    const w = node.hw * 2 + 16;
    const h = NODE_H;
    const isPending = pendingRevealIds.has(node.id);

    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.style.transformOrigin = "0 0";

    if (isPending) {
      // Hidden — will be revealed by animateNodeEntry
      g.style.opacity = "0";
      g.style.transform = `translate(${node.x}px, ${node.y}px) scale(1)`;
    } else {
      g.style.opacity = "1";
      g.style.transform = `translate(${node.x}px, ${node.y}px) scale(1)`;
      g.style.transition = `transform ${MOVE_MS}ms ease, opacity 0.25s ease`;
    }

    // Solid fill — starts transparent for pending, visible for pre-existing
    const fillEl = rc.rectangle(-w / 2, -h / 2, w, h, {
      stroke: "none",
      fill: node.discarded ? "#eee" : "#fff",
      fillStyle: "solid",
      roughness: 1.2,
      seed: seed(node.id + "fill"),
    });
    fillEl.dataset.layer = "fill";
    if (isPending) fillEl.style.opacity = "0";
    g.appendChild(fillEl);

    // Draw 4 sides as individual lines — enables per-side pencil→ink animation
    const x1 = -w / 2,
      y1 = -h / 2;
    const x2 = w / 2,
      y2 = h / 2;
    const sides = [
      { from: [x1, y1], to: [x2, y1], key: "top" },
      { from: [x2, y1], to: [x2, y2], key: "right" },
      { from: [x2, y2], to: [x1, y2], key: "bottom" },
      { from: [x1, y2], to: [x1, y1], key: "left" },
    ];

    for (const side of sides) {
      const sideSeed = seed(node.id + side.key);

      // Pencil: light gray sketch
      const pencil = rc.line(
        side.from[0],
        side.from[1],
        side.to[0],
        side.to[1],
        {
          stroke: "#c0b8a8",
          roughness: 1.2,
          strokeWidth: 1,
          seed: sideSeed,
        },
      );
      pencil.style.opacity = isPending ? "0" : "0.5";
      pencil.dataset.layer = "pencil";
      g.appendChild(pencil);

      // Ink: colored border retrace
      const ink = rc.line(side.from[0], side.from[1], side.to[0], side.to[1], {
        stroke: node.discarded ? "#999" : colors.border,
        roughness: 1.2,
        strokeWidth: 1.8,
        seed: sideSeed + 7,
      });
      ink.dataset.layer = "ink";
      if (isPending) {
        // Hide ink paths — animation will reveal via strokeDashoffset
        ink.querySelectorAll("path").forEach((p) => {
          try {
            const len = p.getTotalLength();
            p.style.strokeDasharray = String(len);
            p.style.strokeDashoffset = String(len);
          } catch {
            /* fill paths — handled by nodeIn */
          }
        });
      }
      g.appendChild(ink);
    }

    // Label text
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", 0);
    text.setAttribute("y", 4);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("font-size", "10");
    text.setAttribute(
      "class",
      `node-label${node.discarded ? " node-label--discarded" : ""}`,
    );
    text.textContent = node.label;
    if (isPending) text.style.opacity = "0";
    g.appendChild(text);

    return { g, origX: node.x, origY: node.y };
  }

  // Incremental draw — only creates rough.js elements for new nodes/edges
  $effect(() => {
    if (!svgEl || layoutResult.nodes.length === 0) return;
    const rc = rough.svg(svgEl);

    const nodeG = svgEl.querySelector(".graph-nodes");
    const edgeG = svgEl.querySelector(".graph-edges");
    const discardG = svgEl.querySelector(".graph-discards");

    // Compute viewBox
    let minX = Infinity,
      minY = Infinity,
      maxX = -Infinity,
      maxY = -Infinity;
    for (const n of layoutResult.nodes) {
      minX = Math.min(minX, n.x - n.hw - 16);
      maxX = Math.max(maxX, n.x + n.hw + 16);
      minY = Math.min(minY, n.y - NODE_H / 2 - 8);
      maxY = Math.max(maxY, n.y + NODE_H / 2 + 8);
    }
    const pad = 20;
    svgEl.setAttribute(
      "viewBox",
      `${minX - pad} ${minY - pad} ${maxX - minX + pad * 2} ${maxY - minY + pad * 2}`,
    );
    svgEl.setAttribute("preserveAspectRatio", "xMidYMin meet");

    // --- Nodes: add new, move existing ---
    const currentIds = new Set(layoutResult.nodes.map((n) => n.id));

    // Remove nodes no longer in layout
    for (const [id, entry] of drawnNodes) {
      if (!currentIds.has(id)) {
        entry.g.remove();
        drawnNodes.delete(id);
      }
    }

    for (const node of layoutResult.nodes) {
      const existing = drawnNodes.get(node.id);
      if (existing) {
        // Slide to new position via CSS transition — no rough.js redraw
        existing.g.style.transform = `translate(${node.x}px, ${node.y}px) scale(1)`;
        existing.origX = node.x;
        existing.origY = node.y;
      } else {
        // New node — draw with rough.js once
        const entry = drawNode(rc, node);
        nodeG.appendChild(entry.g);
        drawnNodes.set(node.id, entry);
      }
    }

    // --- Edges: rough.js pencil + ink with draw animation for new edges ---
    clearChildren(discardG);
    const currentEdgeKeys = new Set();
    const newEdgeEls = new Map();
    const newEdgeIds = []; // edges that need pencil→ink animation

    for (const edge of layoutResult.edges) {
      const from = layoutResult.nodeMap[edge.from];
      const to = layoutResult.nodeMap[edge.to];
      if (from?.x == null || to?.x == null) continue;

      const key = `${edge.from}|${edge.to}`;
      currentEdgeKeys.add(key);
      const edgeColor = EDGE_COLORS[edge.type] || EDGE_COLORS.link;
      const edgeSeed = seed(edge.from + edge.to);

      const existing = drawnEdges.get(key);
      if (existing) {
        // Only redraw if endpoints actually moved
        if (
          existing.fx !== from.x ||
          existing.fy !== from.y ||
          existing.tx !== to.x ||
          existing.ty !== to.y
        ) {
          clearChildren(existing.el);
          fillEdgeGroup(
            rc,
            existing.el,
            from.x,
            from.y,
            to.x,
            to.y,
            edgeColor,
            edgeSeed,
            false,
          );
        }
        existing.fx = from.x;
        existing.fy = from.y;
        existing.tx = to.x;
        existing.ty = to.y;
        newEdgeEls.set(key, existing);
      } else {
        // New edge — create group, draw hidden, queue for animation
        const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
        g.style.transition = "opacity 0.15s";
        fillEdgeGroup(
          rc,
          g,
          from.x,
          from.y,
          to.x,
          to.y,
          edgeColor,
          edgeSeed,
          true,
        );
        edgeG.appendChild(g);
        newEdgeEls.set(key, {
          el: g,
          from: edge.from,
          to: edge.to,
          fx: from.x,
          fy: from.y,
          tx: to.x,
          ty: to.y,
        });
        newEdgeIds.push(key);
      }
    }

    // Remove stale edges
    for (const [key, entry] of drawnEdges) {
      if (!currentEdgeKeys.has(key)) entry.el.remove();
    }
    drawnEdges = newEdgeEls;

    // Queue edge animations — they'll play alongside node reveals
    for (const key of newEdgeIds) {
      pendingEdgeKeys.add(key);
    }

    // Discard marks
    for (const node of layoutResult.nodes) {
      if (!node.discarded) continue;
      const w = node.hw * 2 + 16;
      const h = NODE_H;
      const x1 = node.x - w / 2 + 3;
      const x2 = node.x + w / 2 - 3;
      const y1 = node.y - h / 2 + 3;
      const y2 = node.y + h / 2 - 3;
      discardG.appendChild(
        rc.line(x1, y1, x2, y2, {
          stroke: "#ff3d00",
          strokeWidth: 2,
          roughness: 1.5,
        }),
      );
      discardG.appendChild(
        rc.line(x2, y1, x1, y2, {
          stroke: "#ff3d00",
          strokeWidth: 2,
          roughness: 1.5,
        }),
      );
    }

    // Update refs for dimming effect
    nodeElsForDim = new Map([...drawnNodes].map(([id, e]) => [id, e.g]));
    edgeElsForDim = newEdgeEls;
  });

  // Hover/select dimming — just opacity, no redraw
  $effect(() => {
    const focusId = selectedNode || hoveredNode;
    const connected = getConnectedNodes(focusId);

    for (const [id, el] of nodeElsForDim) {
      if (pendingRevealIds.has(id)) continue; // don't touch nodes mid-reveal
      el.style.opacity = connected && !connected.has(id) ? "0.15" : "1";
    }
    for (const [key, edge] of edgeElsForDim) {
      if (pendingEdgeKeys.has(key)) continue; // don't touch edges mid-reveal
      const dim =
        connected && !connected.has(edge.from) && !connected.has(edge.to);
      edge.el.style.opacity = dim ? "0.1" : "1";
    }
  });
</script>

<svelte:window onkeydown={onGlobalKeydown} />

<svelte:head>
  <title>Knowledge Explorer</title>
</svelte:head>

<div class="explorer">
  <div class="explorer-body">
    <aside class="chat-panel">
      <header class="explorer-header">
        <span class="header-title">KNOWLEDGE</span>
      </header>
      <div class="chat-log" bind:this={chatLog}>
        {#each messages as msg, i}
          <div class="chat-msg chat-msg--{msg.role}">
            <span class="chat-role">{msg.role === "user" ? "you" : "qwen"}</span
            >
            {#if msg.role === "assistant"}
              <span class="chat-text">{@html marked(msg.content)}</span>
            {:else}
              <span class="chat-text">{msg.content}</span>
            {/if}
            {#if i === messages.length - 1 && isStreaming && msg.role === "assistant"}
              <span class="chat-cursor">|</span>
            {/if}
          </div>
        {/each}
        {#if messages.length === 0}
          <p class="chat-empty">
            Ask a question to explore your knowledge graph
          </p>
        {/if}
      </div>
      <div class="chat-input-bar">
        <input
          type="text"
          bind:value={inputText}
          onkeydown={onKeydown}
          placeholder="explore your knowledge..."
          disabled={isStreaming}
        />
        <button
          onclick={sendMessage}
          disabled={isStreaming || !inputText.trim()}
        >
          &rarr;
        </button>
      </div>
    </aside>

    <div class="graph-area">
      {#if layoutResult.nodes.length === 0}
        <p class="graph-empty">Ask a question to start exploring</p>
      {:else}
        <svg
          bind:this={svgEl}
          class="graph-svg"
          xmlns="http://www.w3.org/2000/svg"
        >
          <g class="graph-edges"></g>
          <g class="graph-nodes"></g>
          <g class="graph-discards"></g>
          <g class="graph-hit-areas">
            {#each layoutResult.nodes as node}
              {#if !node.discarded}
                <rect
                  x={node.x - (node.hw + 8)}
                  y={node.y - 22}
                  width={(node.hw + 8) * 2}
                  height={44}
                  fill="transparent"
                  style="cursor: pointer;"
                  role="button"
                  tabindex="0"
                  aria-label={node.label}
                  onmouseenter={() => (hoveredNode = node.id)}
                  onmouseleave={() => (hoveredNode = null)}
                  onclick={() =>
                    (selectedNode = selectedNode === node.id ? null : node.id)}
                />
              {/if}
            {/each}
          </g>
        </svg>
      {/if}
    </div>
  </div>

  {#if selectedNode}
    <div class="note-drawer" role="complementary" aria-label="Note details">
      <button class="drawer-close" onclick={() => (selectedNode = null)}>
        &times;
      </button>
      {#if drawerLoading}
        <p class="drawer-loading">Loading...</p>
      {:else if drawerNote}
        <h2 class="drawer-title">{drawerNote.title}</h2>
        <div class="drawer-meta">
          <span
            class="drawer-type"
            style="background: {TYPE_COLORS[drawerNote.type]?.border ||
              '#ffd54f'}">{drawerNote.type}</span
          >
          {#each drawerNote.tags || [] as tag}
            <span class="drawer-tag">{tag}</span>
          {/each}
        </div>
        <div class="drawer-content">
          {@html marked(cleanNoteContent(drawerNote.content))}
        </div>
        {#if drawerNote.edges?.length > 0}
          <div class="drawer-edges">
            <h3 class="drawer-edges-title">EDGES</h3>
            {#each drawerNote.edges as edge}
              <div class="drawer-edge">
                <span class="drawer-edge-type"
                  >{edge.edge_type || edge.kind || "link"}</span
                >
                <span class="drawer-edge-target"
                  >{edge.target_title || edge.target_id}</span
                >
              </div>
            {/each}
          </div>
        {/if}
      {:else}
        <p class="drawer-loading">Note not found</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  :global(body) {
    margin: 0;
    padding: 0;
  }
  .explorer {
    height: 100vh;
    background: #faf8f4;
    color: #1a1a1a;
  }
  .explorer-body {
    display: flex;
    height: 100%;
  }
  .chat-panel {
    width: 35%;
    min-width: 380px;
    max-width: 600px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    border-right: 2px solid #1a1a1a;
    background: #fffef7;
    font-family: monospace;
    font-size: 13px;
  }
  .explorer-header {
    background: #ffd54f;
    color: #1a1a1a;
    font-family: monospace;
    padding: 4px 12px;
    display: flex;
    align-items: center;
    flex-shrink: 0;
    border-bottom: 2px solid #1a1a1a;
  }
  .header-title {
    font-weight: 700;
    font-size: 12px;
    letter-spacing: 0.15em;
  }
  .chat-log {
    flex: 1 1 0;
    overflow-y: auto;
    padding: 10px 12px;
    min-height: 0;
  }
  .chat-msg {
    margin-bottom: 8px;
    line-height: 1.45;
  }
  .chat-msg--user {
    text-align: right;
  }
  .chat-msg--assistant {
    border-left: 3px solid #ffd54f;
    padding-left: 10px;
  }
  .chat-role {
    font-weight: 700;
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.05em;
    display: block;
    margin-bottom: 1px;
    opacity: 0.5;
  }
  .chat-text {
    word-break: break-word;
    display: block;
  }
  .chat-msg--user .chat-text {
    white-space: pre-wrap;
  }
  .chat-text :global(h1),
  .chat-text :global(h2),
  .chat-text :global(h3) {
    font-size: 13px;
    font-weight: 700;
    margin: 6px 0 3px 0;
  }
  .chat-text :global(p) {
    margin: 3px 0;
  }
  .chat-text :global(ul),
  .chat-text :global(ol) {
    padding-left: 18px;
    margin: 3px 0;
  }
  .chat-text :global(li) {
    margin: 2px 0;
  }
  .chat-text :global(code) {
    background: #e5e0d8;
    padding: 1px 3px;
    font-size: 12px;
  }
  .chat-text :global(pre) {
    background: #1a1a1a;
    color: #f5f0e8;
    padding: 6px;
    overflow-x: auto;
    font-size: 12px;
    margin: 3px 0;
  }
  .chat-text :global(pre code) {
    background: none;
    padding: 0;
    color: inherit;
  }
  .chat-text :global(strong) {
    font-weight: 700;
  }
  .chat-cursor {
    animation: blink 0.8s step-end infinite;
  }
  @keyframes blink {
    50% {
      opacity: 0;
    }
  }
  .chat-empty {
    text-align: center;
    opacity: 0.4;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-size: 12px;
    padding: 12px;
  }
  .chat-input-bar {
    border-top: 1.5px solid #1a1a1a;
    display: flex;
  }
  .chat-input-bar input {
    flex: 1;
    padding: 12px;
    font-family: monospace;
    font-size: 13px;
    border: none;
    background: transparent;
    outline: none;
  }
  .chat-input-bar button {
    padding: 12px 14px;
    font-family: monospace;
    font-weight: 700;
    font-size: 14px;
    border: none;
    border-left: 1.5px solid #1a1a1a;
    background: transparent;
    cursor: pointer;
  }
  .chat-input-bar button:disabled {
    opacity: 0.3;
    cursor: default;
  }
  .graph-area {
    flex: 1;
    min-height: 0;
    overflow: hidden;
    position: relative;
  }
  .graph-empty {
    font-family: monospace;
    text-align: center;
    padding: 40px;
    opacity: 0.4;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-size: 13px;
  }
  .graph-svg {
    width: 100%;
    height: 100%;
  }
  .graph-svg :global(path) {
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .node-label {
    font-family: monospace;
    font-weight: 700;
    fill: #1a1a1a;
    pointer-events: none;
  }
  .node-label--discarded {
    text-decoration: line-through;
    opacity: 0.4;
  }
  /* Pencil→ink draw animation keyframes (global — targets dynamic SVG elements) */
  @keyframes -global-edgeDraw {
    to {
      stroke-dashoffset: 0;
    }
  }
  @keyframes -global-nodeIn {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
  @keyframes -global-textJot {
    0% {
      opacity: 0;
      transform: translate(-1px, 0.5px);
    }
    40% {
      opacity: 0.85;
    }
    100% {
      opacity: 1;
      transform: translate(0, 0);
    }
  }
  .note-drawer {
    position: fixed;
    right: 0;
    top: 0;
    bottom: 0;
    width: 75vw;
    background: #fffef7;
    border-left: 2px solid #1a1a1a;
    overflow-y: auto;
    padding: 1.5rem;
    font-family: monospace;
    z-index: 100;
    box-shadow: -4px 0 12px rgba(0, 0, 0, 0.1);
    animation: slideIn 200ms ease-out;
  }
  @keyframes slideIn {
    from {
      transform: translateX(100%);
    }
    to {
      transform: translateX(0);
    }
  }
  .drawer-close {
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    background: none;
    border: none;
    font-size: 1.5rem;
    cursor: pointer;
    font-family: monospace;
    line-height: 1;
  }
  .drawer-title {
    font-size: 1.1rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 0 0 0.75rem 0;
  }
  .drawer-meta {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }
  .drawer-type {
    color: #1a1a1a;
    padding: 0.15rem 0.5rem;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
  }
  .drawer-tag {
    background: #e5e0d8;
    padding: 0.15rem 0.5rem;
    font-size: 0.75rem;
  }
  .drawer-content {
    font-size: 0.85rem;
    line-height: 1.6;
    border-top: 1px solid #c0b8a8;
    padding-top: 1rem;
  }
  .drawer-content :global(h1),
  .drawer-content :global(h2),
  .drawer-content :global(h3) {
    font-size: 0.95rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    margin: 1rem 0 0.5rem 0;
  }
  .drawer-content :global(p) {
    margin: 0.5rem 0;
  }
  .drawer-content :global(code) {
    background: #e5e0d8;
    padding: 0.1rem 0.3rem;
    font-size: 0.8rem;
  }
  .drawer-content :global(pre) {
    background: #1a1a1a;
    color: #f5f0e8;
    padding: 0.75rem;
    overflow-x: auto;
    font-size: 0.8rem;
    margin: 0.5rem 0;
  }
  .drawer-content :global(pre code) {
    background: none;
    padding: 0;
    color: inherit;
    font-size: inherit;
  }
  .drawer-content :global(ol),
  .drawer-content :global(ul) {
    padding-left: 1.5rem;
    margin: 0.5rem 0;
  }
  .drawer-content :global(li) {
    margin: 0.25rem 0;
  }
  .drawer-content :global(table) {
    width: 100%;
    border-collapse: collapse;
    margin: 0.75rem 0;
    font-size: 0.8rem;
  }
  .drawer-content :global(th),
  .drawer-content :global(td) {
    border: 1px solid #c0b8a8;
    padding: 0.4rem 0.6rem;
    text-align: left;
  }
  .drawer-content :global(th) {
    background: #f0ebe3;
    font-weight: 700;
    text-transform: uppercase;
    font-size: 0.75rem;
    letter-spacing: 0.03em;
  }
  .drawer-content :global(tr:nth-child(even)) {
    background: #f8f5ef;
  }
  .drawer-edges {
    border-top: 1px solid #c0b8a8;
    padding-top: 1rem;
    margin-top: 1rem;
  }
  .drawer-edges-title {
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    margin: 0 0 0.5rem 0;
    opacity: 0.5;
  }
  .drawer-edge {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    padding: 0.25rem 0;
    font-size: 0.8rem;
  }
  .drawer-edge-type {
    opacity: 0.5;
    min-width: 80px;
  }
  .drawer-loading {
    text-align: center;
    opacity: 0.4;
    padding: 2rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
</style>
