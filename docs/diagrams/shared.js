(function(){
  'use strict';

  var wrap = document.getElementById('diagramContainer');
  if (!wrap) return;

  var svg = null;
  var scale = 1, tx = 0, ty = 0;
  var dragging = false, sx = 0, sy = 0;

  // Tooltip element
  var tip = document.createElement('div');
  tip.className = 'tooltip';
  document.body.appendChild(tip);

  function update() {
    if (!svg) return;
    svg.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
    var el = document.getElementById('zoomLevel');
    if (el) el.textContent = Math.round(scale * 100) + '%';
  }

  function getContainerRect() {
    return wrap.getBoundingClientRect();
  }

  function getViewBox() {
    if (!svg) return null;
    var vb = svg.getAttribute('viewBox');
    if (vb) {
      var p = vb.split(/\s+/).map(Number);
      if (p.length >= 4 && p[2] > 0 && p[3] > 0) return { w: p[2], h: p[3] };
    }
    // Fallback: measure rendered size
    var bbox = svg.getBBox();
    if (bbox && bbox.width > 0) return { w: bbox.width, h: bbox.height };
    return svg.width && svg.height.baseVal ? { w: svg.width.baseVal.value, h: svg.height.baseVal.value } : null;
  }

  function centerDiagram() {
    if (!svg) return;
    var vb = getViewBox();
    if (!vb || vb.w === 0 || vb.h === 0) return;
    var rect = getContainerRect();
    if (rect.width === 0 || rect.height === 0) return;
    var pad = 20;
    var availW = rect.width - pad * 2;
    var availH = rect.height - pad * 2;
    if (availW <= 0 || availH <= 0) return;
    var s = Math.min(1.5, Math.max(0.25, availW / vb.w));
    var sH = availH / vb.h;
    if (s * vb.h > availH) s = Math.min(s, sH);
    scale = s;
    tx = (rect.width - vb.w * scale) / 2;
    ty = (rect.height - vb.h * scale) / 2;
    update();
  }

  // Expose globally for onclick handlers
  window.zoomIn = function() {
    if (!svg) return;
    scale = Math.min(6, scale * 1.25);
    update();
  };
  window.zoomOut = function() {
    if (!svg) return;
    scale = Math.max(0.2, scale * 0.8);
    update();
  };
  window.zoomReset = function() {
    centerDiagram();
  };
  // Hover tooltip for mermaid nodes
  function setupTooltips() {
    var nodes = svg.querySelectorAll('.node');
    nodes.forEach(function(node) {
      var title = node.querySelector('title');
      if (!title || !title.textContent) return;
      var label = title.textContent.trim();
      // Only show tooltip for nodes with extra info (pipe-separated)
      if (label.indexOf('|') === -1 && label.indexOf('\n') === -1) return;
      node.style.cursor = 'pointer';
      node.addEventListener('mouseenter', function(e) {
        var text = label.replace(/\|/g, '\n').replace(/\s+/g, ' ').trim();
        tip.textContent = text;
        tip.style.display = 'block';
      });
      node.addEventListener('mousemove', function(e) {
        tip.style.left = (e.clientX + 14) + 'px';
        tip.style.top = (e.clientY + 14) + 'px';
      });
      node.addEventListener('mouseleave', function() {
        tip.style.display = 'none';
      });
    });
  }

  mermaid.initialize({startOnLoad: false, securityLevel: 'loose', theme: 'base'});

  document.addEventListener('DOMContentLoaded', async function() {
    await mermaid.run({ querySelector: '.mermaid' });
    svg = document.querySelector('#diagramContainer svg');
    if (!svg) return;

    svg.style.cursor = 'grab';

    // Mouse drag for pan
    svg.addEventListener('mousedown', function(e) {
      dragging = true;
      sx = e.clientX - tx;
      sy = e.clientY - ty;
      svg.style.cursor = 'grabbing';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      tx = e.clientX - sx;
      ty = e.clientY - sy;
      update();
    });
    document.addEventListener('mouseup', function() {
      if (dragging) {
        dragging = false;
        if (svg) svg.style.cursor = 'grab';
      }
    });

    // Scroll zoom (mouse wheel)
    wrap.addEventListener('wheel', function(e) {
      e.preventDefault();
      var rect = getContainerRect();
      var mx = e.clientX - rect.left;
      var my = e.clientY - rect.top;
      var f = e.deltaY > 0 ? 0.9 : 1.1;
      var ns = Math.max(0.2, Math.min(6, scale * f));
      tx = mx - (mx - tx) * (ns / scale);
      ty = my - (my - ty) * (ns / scale);
      scale = ns;
      update();
    }, { passive: false });

    // Touch support for mobile
    var lastDist = 0, lastTouch = null;
    wrap.addEventListener('touchstart', function(e) {
      if (e.touches.length === 1) {
        lastTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      } else if (e.touches.length === 2) {
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        lastDist = Math.sqrt(dx * dx + dy * dy);
      }
    }, { passive: true });
    wrap.addEventListener('touchmove', function(e) {
      if (e.touches.length === 1 && lastTouch) {
        var dx = e.touches[0].clientX - lastTouch.x;
        var dy = e.touches[0].clientY - lastTouch.y;
        tx += dx; ty += dy;
        lastTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY };
        update();
      } else if (e.touches.length === 2) {
        var dx2 = e.touches[0].clientX - e.touches[1].clientX;
        var dy2 = e.touches[0].clientY - e.touches[1].clientY;
        var dist = Math.sqrt(dx2 * dx2 + dy2 * dy2);
        if (lastDist > 0) {
          var f2 = dist / lastDist;
          var ns2 = Math.max(0.2, Math.min(6, scale * f2));
          scale = ns2;
          update();
        }
        lastDist = dist;
      }
    }, { passive: true });
    wrap.addEventListener('touchend', function() {
      lastDist = 0;
      lastTouch = null;
    }, { passive: true });

    // Center diagram with smart initial scale
    centerDiagram();

    // Re-center on resize
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function() { centerDiagram(); });
      ro.observe(wrap);
    }

    setupTooltips();
  });
})();
