/* 照片地图的公共脚本：按屏幕像素距离聚合标记 + 胶卷视图 + 大图灯箱。
   各地图模板（Leaflet / Google Maps / 百度地图）共用这一份逻辑。 */
(function (global) {
  'use strict';

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function esc(value) {
    return String(value === undefined || value === null ? '' : value).replace(
      /[&<>"']/g,
      function (c) { return ESCAPES[c]; }
    );
  }

  function num(value, digits) {
    var n = Number(value);
    return isFinite(n) ? n.toFixed(digits === undefined ? 6 : digits) : '';
  }

  function coordText(lat, lon, digits) {
    var a = num(lat, digits);
    var b = num(lon, digits);
    return a === '' ? '' : a + ', ' + b;
  }

  /* Escape 只交给最上层的 UI（灯箱 > 胶卷）。在 capture 阶段先定归属，
     这样与各组件 keydown 监听器的注册顺序无关。 */
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (document.querySelector('.pm-backdrop.pm-open')) event.__pmEscOwner = 'lightbox';
    else if (document.querySelector('.pm-strip.pm-open')) event.__pmEscOwner = 'strip';
  }, true);

  /* 同一格子里照片过多时（坐标几乎完全相同）整格合并成一簇，
     否则两两比较会退化成 O(n²)。 */
  var MAX_COMPARE = 64;

  function withinGap(a, b, gap) {
    var dx = a.x - b.x;
    var dy = a.y - b.y;
    return dx * dx + dy * dy < gap * gap;
  }

  /* 按屏幕像素距离聚合：桶宽 = gap，只查 3×3 邻桶 + 单链合并（连通分量）。
     两点的像素距离只随 zoom 变化、与平移无关，所以只需在缩放结束后重算。 */
  function clusterByPixel(positions, gap) {
    var n = positions.length;
    var parent = new Array(n);
    var i;

    for (i = 0; i < n; i++) parent[i] = i;

    function find(x) {
      while (parent[x] !== x) {
        parent[x] = parent[parent[x]];
        x = parent[x];
      }
      return x;
    }

    function union(a, b) {
      var ra = find(a);
      var rb = find(b);
      if (ra === rb) return;
      if (ra < rb) parent[rb] = ra; else parent[ra] = rb;
    }

    var buckets = {};
    for (i = 0; i < n; i++) {
      var p = positions[i];
      if (!p) continue;
      var key = Math.floor(p.x / gap) + ':' + Math.floor(p.y / gap);
      if (!buckets[key]) buckets[key] = [];
      buckets[key].push(i);
    }

    Object.keys(buckets).forEach(function (key) {
      var list = buckets[key];
      var a, b, k;

      if (list.length > MAX_COMPARE) {
        for (k = 1; k < list.length; k++) union(list[0], list[k]);
      } else {
        for (a = 0; a < list.length; a++) {
          for (b = a + 1; b < list.length; b++) {
            if (withinGap(positions[list[a]], positions[list[b]], gap)) union(list[a], list[b]);
          }
        }
      }

      var parts = key.split(':');
      var bx = Number(parts[0]);
      var by = Number(parts[1]);
      for (var dx = -1; dx <= 1; dx++) {
        for (var dy = -1; dy <= 1; dy++) {
          if (!dx && !dy) continue;
          /* 只处理字典序更大的邻桶，避免同一对比较两次 */
          var okey = (bx + dx) + ':' + (by + dy);
          if (okey <= key) continue;
          var other = buckets[okey];
          if (!other) continue;
          if (other.length > MAX_COMPARE) {
            for (k = 0; k < other.length; k++) union(list[0], other[k]);
            continue;
          }
          for (a = 0; a < list.length; a++) {
            for (b = 0; b < other.length; b++) {
              if (withinGap(positions[list[a]], positions[other[b]], gap)) union(list[a], other[b]);
            }
          }
        }
      }
    });

    var groups = {};
    var result = [];
    for (i = 0; i < n; i++) {
      var root = find(i);
      if (!groups[root]) groups[root] = [];
      groups[root].push(i);
    }
    Object.keys(groups).forEach(function (root) { result.push(groups[root]); });
    return result;
  }

  /* 照片的身份：优先用唯一的标记图路径，退化到文件名，再带上坐标。
     服务端每次轮询都会返回全新的对象，所以只能按内容比对。 */
  function photoKey(p) {
    if (!p) return '';
    return (p.marker || p.name || '') + '@' + num(p.lat, 7) + ',' + num(p.lon, 7);
  }

  /* 坐标是否可用：地图 SDK 遇到非法坐标会直接抛异常，
     那会中断整个页面的脚本，所以先自己挡一道。 */
  function validLatLng(lat, lon) {
    return typeof lat === 'number' && typeof lon === 'number' &&
      isFinite(lat) && isFinite(lon);
  }

  function samePhotos(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (photoKey(a[i]) !== photoKey(b[i])) return false;
    }
    return true;
  }

  /* 聚合视图：每张照片一个常驻标记，缩放后重算簇，
     每簇只显示「离簇中心最近」的那张作为代表，其余隐藏；
     代表标记右下角叠一个数量角标。 */
  /* 放大到这一级且簇内不超过 SPIDER_MAX 张时，散开成独立标记 */
  var SPIDER_ZOOM = 15;
  var SPIDER_MAX = 16;

  function ClusterView(photos, adapter, options) {
    options = options || {};
    this.photos = photos || [];
    this.adapter = adapter;
    this.gap = options.gap || 80;
    this.onSelect = options.onSelect || function () {};
    /* 单张照片的标记可以被拖动改位置（只有 live 模式给这个回调） */
    this.onDrag = options.onDrag || null;
    this.markers = [];
    this.clusters = [];
    this.badges = [];
    this.init();
  }

  ClusterView.prototype.init = function () {
    var self = this;

    this.layer = document.createElement('div');
    this.layer.className = 'pm-cluster-layer';
    document.body.appendChild(this.layer);

    this.markers = [];
    this.photos.forEach(function (p, i) {
      /* 坐标缺失的照片画不出标记，用 null 占位保持下标对应，
         否则创建标记时抛错会把整个页面的脚本都中断掉。 */
      if (!validLatLng(p.lat, p.lon)) {
        self.markers.push(null);
        return;
      }
      var handle = self.adapter.createMarker(p.lat, p.lon, p, function () { self.select(i); });
      self.markers.push(handle);
    });

    /* 地图事件的解绑函数由 adapter 返回，destroy() 时用来摘掉监听 */
    this.unbindZoom = this.adapter.onZoomChange(function () { self.update(); }) || null;
    this.unbindMove = this.adapter.onMove(function () { self.syncBadges(); }) || null;
    this.onResize = function () {
      setTimeout(function () { self.update(); }, 150);
    };
    window.addEventListener('resize', this.onResize);

    this.update();
  };

  /* 销毁视图：移除所有标记、角标层与地图事件监听。 */
  ClusterView.prototype.destroy = function () {
    var self = this;
    this.markers.forEach(function (handle) {
      if (handle && self.adapter.removeMarker) self.adapter.removeMarker(handle);
    });
    this.markers = [];
    this.clusters = [];
    this.badges = [];
    if (this.layer && this.layer.parentNode) this.layer.parentNode.removeChild(this.layer);
    this.layer = null;
    if (this.unbindZoom) { this.unbindZoom(); this.unbindZoom = null; }
    if (this.unbindMove) { this.unbindMove(); this.unbindMove = null; }
    if (this.onResize) { window.removeEventListener('resize', this.onResize); this.onResize = null; }
  };

  /* 替换照片列表：内容没变只重算聚类，变了就整体重建标记。 */
  ClusterView.prototype.setPhotos = function (photos) {
    photos = photos || [];
    if (samePhotos(this.photos, photos)) { this.update(); return; }
    this.destroy();
    this.photos = photos;
    this.init();
  };

  ClusterView.prototype.update = function () {
    var self = this;
    var positions = this.photos.map(function (p) {
      if (!validLatLng(p.lat, p.lon)) return null;
      var pt = self.adapter.toPixel(p.lat, p.lon);
      return pt ? { x: pt.x, y: pt.y } : null;
    });

    var clusters = [];
    clusterByPixel(positions, this.gap).forEach(function (idxs) {
      var cx = 0, cy = 0, count = 0;
      idxs.forEach(function (i) {
        var pt = positions[i];
        if (!pt) return;
        cx += pt.x; cy += pt.y; count++;
      });
      if (count) { cx /= count; cy /= count; }

      var rep = idxs[0];
      var best = Infinity;
      idxs.forEach(function (i) {
        var pt = positions[i];
        if (!pt) return;
        var d = Math.hypot(pt.x - cx, pt.y - cy);
        if (d < best) { best = d; rep = i; }
      });

      var centerLat = self.photos[rep].lat;
      var centerLon = self.photos[rep].lon;
      if (count > 1) {
        var ll = self.adapter.fromPixel(cx, cy);
        if (ll) { centerLat = ll.lat; centerLon = ll.lon; }
      }

      /* 放得够大时，把「挤在同一小块地方」的少数几张拆成一圈单独显示，
         这样每张都能单独点开、单独拖动；照片太多时拆开反而没法看，仍按整体处理。 */
      var zoom = self.adapter.getZoom ? self.adapter.getZoom() : 0;
      var spider = (
        idxs.length > 1 &&
        idxs.length <= SPIDER_MAX &&
        zoom >= SPIDER_ZOOM
      );

      if (spider) {
        var n = idxs.length;
        var radius = Math.max(72, (n * 76) / (2 * Math.PI));
        idxs.forEach(function (i, k) {
          var angle = (2 * Math.PI * k) / n - Math.PI / 2;
          var point = self.adapter.fromPixel(
            cx + radius * Math.cos(angle),
            cy + radius * Math.sin(angle)
          );
          if (!point) {
            self.adapter.setMarkerVisible(self.markers[i], false);
            return;
          }
          self.adapter.setMarkerPosition(self.markers[i], point.lat, point.lon);
          self.adapter.setMarkerVisible(self.markers[i], true);
          if (self.adapter.setMarkerDraggable) {
            self.adapter.setMarkerDraggable(
              self.markers[i],
              !!self.onDrag,
              self.onDrag ? (function (index) {
                return function (drop) { self.onDrag(index, drop); };
              })(i) : null
            );
          }
        });
      } else {
        idxs.forEach(function (i) {
          if (i === rep) {
            self.adapter.setMarkerPosition(self.markers[i], centerLat, centerLon);
            self.adapter.setMarkerVisible(self.markers[i], true);
          } else {
            self.adapter.setMarkerVisible(self.markers[i], false);
          }
        });

        /* 只有「一张照片独占一个标记」时才能拖：
           聚类代表标记代表好几张，拖它该移到哪张说不清。 */
        if (self.adapter.setMarkerDraggable) {
          var single = idxs.length === 1 && !!self.onDrag;
          self.adapter.setMarkerDraggable(
            self.markers[rep],
            single,
            single ? function (point) { self.onDrag(rep, point); } : null
          );
        }
      }

      clusters.push({
        indices: idxs,
        rep: rep,
        size: idxs.length,
        centerLat: centerLat,
        centerLon: centerLon,
        spidered: !!spider
      });
    });

    this.clusters = clusters;
    this.syncBadges();
  };

  /* 角标跟随地图移动：用簇中心的经纬度重新换算当前像素，
     避免用缩放时算出的旧像素坐标（平移后会偏移）。 */
  ClusterView.prototype.syncBadges = function () {
    var self = this;
    var rect = this.adapter.container.getBoundingClientRect();
    var used = 0;

    this.clusters.forEach(function (c) {
      if (c.size < 2 || c.spidered) return;
      var pt = self.adapter.toPixel(c.centerLat, c.centerLon);
      if (!pt) return;

      var badge = self.badges[used];
      if (!badge) {
        badge = document.createElement('div');
        badge.className = 'pm-badge';
        self.layer.appendChild(badge);
        self.badges.push(badge);
      }
      badge.textContent = String(c.size);
      badge.style.display = 'block';
      badge.style.left = (rect.left + pt.x + 20) + 'px';
      badge.style.top = (rect.top + pt.y + 20) + 'px';
      used++;
    });

    for (var i = used; i < this.badges.length; i++) this.badges[i].style.display = 'none';
  };

  ClusterView.prototype.select = function (photoIndex) {
    var cluster = null;
    this.clusters.forEach(function (c) {
      if (!cluster && c.indices.indexOf(photoIndex) !== -1) cluster = c;
    });
    if (!cluster) cluster = { indices: [photoIndex], size: 1 };
    /* 散开的簇里点哪张就是哪张，直接开灯箱而不是弹胶卷 */
    if (cluster.spidered) {
      this.onSelect(photoIndex, [photoIndex]);
      return;
    }
    this.onSelect(photoIndex, cluster.indices);
  };

  /* 待确认定位的照片：位置还没定下来，先按推断出的候选位置显示，
     角标是个「?」，提醒用户这里只是猜测。
     这些标记不参与聚类——位置本来就不确定，聚在一起没有意义。 */
  function PendingView(photos, adapter, options) {
    options = options || {};
    this.photos = photos || [];
    this.adapter = adapter;
    this.onSelect = options.onSelect || function () {};
    this.markers = [];
    this.badges = [];
    this.init();
  }

  PendingView.prototype.init = function () {
    var self = this;

    this.layer = document.createElement('div');
    this.layer.className = 'pm-cluster-layer';
    document.body.appendChild(this.layer);

    /* 没有候选位置的照片画不出标记（位置是空的），
       用 null 占位保持下标与 photos 一一对应。 */
    this.markers = [];
    this.photos.forEach(function (p, i) {
      var point = p.candidate;
      if (!point || !validLatLng(point.lat, point.lon)) {
        self.markers.push(null);
        return;
      }
      /* 待确认的「?」标记压在已定位照片下面：
         同一位置有真有假时，点击优先给已定位的那张 */
      var handle = self.adapter.createMarker(
        point.lat, point.lon, p, function () { self.select(i); }, -1000
      );
      self.markers.push(handle);
    });

    this.unbindMove = this.adapter.onMove(function () { self.syncBadges(); }) || null;
    this.syncBadges();
  };

  PendingView.prototype.syncBadges = function () {
    var self = this;
    var rect = this.adapter.container.getBoundingClientRect();
    var used = 0;

    this.photos.forEach(function (p) {
      var point = p.candidate;
      if (!point || !validLatLng(point.lat, point.lon)) return;
      var pt = self.adapter.toPixel(point.lat, point.lon);
      if (!pt) return;

      var badge = self.badges[used];
      if (!badge) {
        badge = document.createElement('div');
        badge.className = 'pm-badge pm-pending-badge';
        badge.textContent = '?';
        badge.title = '位置待确认';
        self.layer.appendChild(badge);
        self.badges.push(badge);
      }
      badge.style.display = 'block';
      badge.style.left = (rect.left + pt.x + 20) + 'px';
      badge.style.top = (rect.top + pt.y + 20) + 'px';
      used++;
    });

    for (var i = used; i < this.badges.length; i++) this.badges[i].style.display = 'none';
  };

  PendingView.prototype.select = function (photoIndex) {
    this.onSelect(photoIndex, this.photos[photoIndex]);
  };

  PendingView.prototype.destroy = function () {
    var self = this;
    this.markers.forEach(function (handle) {
      if (handle && self.adapter.removeMarker) self.adapter.removeMarker(handle);
    });
    this.markers = [];
    this.badges = [];
    if (this.layer && this.layer.parentNode) this.layer.parentNode.removeChild(this.layer);
    this.layer = null;
    if (this.unbindMove) { this.unbindMove(); this.unbindMove = null; }
  };

  PendingView.prototype.setPhotos = function (photos) {
    photos = photos || [];
    var same = photos.length === this.photos.length;
    if (same) {
      for (var i = 0; i < photos.length; i++) {
        var a = photos[i].candidate;
        var b = this.photos[i].candidate;
        var aLat = a ? a.lat : null;
        var bLat = b ? b.lat : null;
        if (photoKey(photos[i]) !== photoKey(this.photos[i]) || aLat !== bLat) {
          same = false;
          break;
        }
      }
    }
    if (same) { this.syncBadges(); return; }
    this.destroy();
    this.photos = photos;
    this.init();
  };

  /* 胶卷：底部横向排列某一簇的全部缩略图，点其中一张打开灯箱。 */
  function Filmstrip(photos, options) {
    options = options || {};
    this.photos = photos || [];
    this.onPick = options.onPick || function () {};
    this.onClose = options.onClose || function () {};
    /* 胶卷里多选照片、一键移动到某个点（只有 serve 模式给这个能力） */
    this.onMoveSelected = options.onMoveSelected || null;
    this.canMove = !!options.canMove && !!this.onMoveSelected;
    this.indices = [];
    this.rendered = [];
    this.keys = [];
    this.selecting = false;
    this.selected = {};
    this.build();
    this.bind();
  }

  Filmstrip.prototype.build = function () {
    var root = document.createElement('div');
    root.className = 'pm-strip';
    var bar = this.canMove ? [
      '<div class="pm-strip-bar">',
      '  <span class="pm-strip-info"></span>',
      '  <button type="button" class="pm-strip-btn pm-strip-move">改变位置</button>',
      '  <button type="button" class="pm-strip-btn pm-strip-all" hidden>全选</button>',
      '  <button type="button" class="pm-strip-btn pm-strip-apply" hidden>移动到…</button>',
      '  <button type="button" class="pm-strip-btn pm-strip-cancel" hidden>取消</button>',
      '</div>'
    ].join('') : '';
    root.innerHTML = [
      '<button type="button" class="pm-strip-close" title="关闭">×</button>',
      bar,
      '<div class="pm-strip-track"></div>'
    ].join('');
    document.body.appendChild(root);
    this.el = root;
    this.track = root.querySelector('.pm-strip-track');
    this.info = root.querySelector('.pm-strip-info');
    this.moveBtn = root.querySelector('.pm-strip-move');
    this.allBtn = root.querySelector('.pm-strip-all');
    this.applyBtn = root.querySelector('.pm-strip-apply');
    this.cancelBtn = root.querySelector('.pm-strip-cancel');
  };

  Filmstrip.prototype.bind = function () {
    var self = this;

    this.el.querySelector('.pm-strip-close').addEventListener('click', function () {
      self.close();
    });

    if (this.canMove) {
      this.moveBtn.addEventListener('click', function () { self.enterSelect(); });
      this.allBtn.addEventListener('click', function () { self.toggleAll(); });
      this.applyBtn.addEventListener('click', function () { self.applySelection(); });
      this.cancelBtn.addEventListener('click', function () { self.exitSelect(); });
    }

    this.track.addEventListener('click', function (event) {
      var node = event.target;
      while (node && node !== self.track && !node.classList.contains('pm-strip-item')) {
        node = node.parentNode;
      }
      if (!node || node === self.track) return;
      var k = Number(node.dataset.index);
      /* 用 rendered（当前照片表里的真实下标）而不是打开时的旧下标：
         照片列表会随定位/轮询整体替换，旧下标早就漂了 */
      if (!isFinite(k) || self.rendered[k] === undefined) return;
      /* 选择态下点缩略图 = 勾选/取消；平时 = 打开灯箱 */
      if (self.selecting) { self.toggle(k); return; }
      self.onPick(k, self.rendered[k], self.rendered.slice());
    });

    /* 胶卷是横向排的：把鼠标滚轮的纵向滚动换成横向滚动，
       照片多到超出屏幕时不用去抓滚动条 */
    this.track.addEventListener('wheel', function (event) {
      if (!self.el.classList.contains('pm-open')) return;
      if (self.track.scrollWidth <= self.track.clientWidth + 1) return;
      var delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
      if (!delta) return;
      event.preventDefault();
      self.track.scrollLeft += delta;
    }, { passive: false });

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (event.__pmEscOwner && event.__pmEscOwner !== 'strip') return;
      if (!self.el.classList.contains('pm-open')) return;
      /* Esc 先退出选择态，再关胶卷 */
      if (self.selecting) { self.exitSelect(); return; }
      self.close();
    });
  };

  /* 胶卷里记的是照片的业务 key（serve 模式是服务端 key，静态导出退到 marker+坐标），
     不是数组下标——照片列表一换，下标就指向别的照片了。 */
  function stripKey(p) {
    return (p && (p.key || photoKey(p))) || '';
  }

  Filmstrip.prototype.open = function (indices) {
    var self = this;
    this.exitSelect();
    this.indices = indices || [];
    this.keys = this.indices
      .map(function (i) { return stripKey(self.photos[i]); })
      .filter(Boolean);
    this.render();
  };

  /* 按当前的 photos 重新定位每一项；找不到的（照片被删了）直接跳过 */
  Filmstrip.prototype.render = function () {
    var self = this;
    this.track.innerHTML = '';
    this.rendered = [];
    this.keys.forEach(function (key) {
      var index = self.photos.findIndex(function (p) { return stripKey(p) === key; });
      if (index < 0) return;
      var p = self.photos[index];
      var fig = document.createElement('figure');
      fig.className = 'pm-strip-item';
      fig.dataset.index = String(self.rendered.length);
      fig.dataset.key = key;
      fig.title = p.name || '';
      var img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.src = p.thumb || p.marker || '';
      img.alt = p.name || '';
      fig.appendChild(img);
      self.track.appendChild(fig);
      self.rendered.push(index);
    });
    this.el.classList.add('pm-open');
    this.syncSelection();
  };

  Filmstrip.prototype.close = function () {
    this.exitSelect();
    this.el.classList.remove('pm-open');
    this.onClose();
  };

  /* 更新胶卷引用的照片表；打开状态下要重画缩略图。 */
  Filmstrip.prototype.setPhotos = function (photos) {
    this.photos = photos || [];
    if (this.el.classList.contains('pm-open')) this.render();
  };

  /* -- 多选移动 --------------------------------------------------------- */

  Filmstrip.prototype.enterSelect = function () {
    this.selecting = true;
    this.selected = {};
    this.syncSelection();
  };

  Filmstrip.prototype.exitSelect = function () {
    if (!this.canMove) return;
    this.selecting = false;
    this.selected = {};
    this.syncSelection();
  };

  Filmstrip.prototype.toggle = function (k) {
    if (this.selected[k]) delete this.selected[k];
    else this.selected[k] = true;
    this.syncSelection();
  };

  Filmstrip.prototype.toggleAll = function () {
    var self = this;
    var all = this.selectedCount() < this.rendered.length;
    this.selected = {};
    if (all) this.rendered.forEach(function (_, k) { self.selected[k] = true; });
    this.syncSelection();
  };

  Filmstrip.prototype.selectedCount = function () {
    var self = this;
    var count = 0;
    this.rendered.forEach(function (_, k) { if (self.selected[k]) count++; });
    return count;
  };

  Filmstrip.prototype.selectedIndices = function () {
    var self = this;
    var out = [];
    this.rendered.forEach(function (photoIndex, k) {
      if (self.selected[k]) out.push(photoIndex);
    });
    return out;
  };

  Filmstrip.prototype.syncSelection = function () {
    var self = this;
    if (!this.canMove) return;
    var count = this.selectedCount();
    var total = this.rendered.length;
    this.el.classList.toggle('pm-selecting', this.selecting);
    this.moveBtn.hidden = this.selecting;
    this.allBtn.hidden = !this.selecting;
    this.applyBtn.hidden = !this.selecting;
    this.cancelBtn.hidden = !this.selecting;
    this.applyBtn.disabled = count === 0;
    this.applyBtn.textContent = count ? '移动到所选（' + count + '）' : '移动到…';
    this.allBtn.textContent = count && count === total ? '取消全选' : '全选';
    this.info.textContent = this.selecting
      ? '点缩略图勾选要移动的照片'
      : (this.photos.length ? '' : '');
    Array.prototype.forEach.call(this.track.children, function (fig) {
      var k = Number(fig.dataset.index);
      fig.classList.toggle('pm-selected', !!self.selected[k]);
    });
  };

  Filmstrip.prototype.applySelection = function () {
    var indices = this.selectedIndices();
    if (!indices.length || !this.onMoveSelected) return;
    var handler = this.onMoveSelected;
    this.exitSelect();
    handler(indices);
  };

  function Lightbox(photos, options) {
    options = options || {};
    this.allPhotos = photos || [];
    this.photos = this.allPhotos;
    this.index = 0;
    this.scale = 1;
    this.panX = 0;
    this.panY = 0;
    this.coordLabel = options.coordLabel || 'WGS-84';
    this.mapLabel = options.mapLabel || '';
    this.build();
    this.bind();
  }

  Lightbox.prototype.build = function () {
    var root = document.createElement('div');
    root.className = 'pm-backdrop';
    root.innerHTML = [
      '<div class="pm-stage"><img class="pm-full" alt="" /></div>',
      '<div class="pm-panel">',
      '  <div class="pm-info"></div>',
      '  <div class="pm-tools">',
      '    <button type="button" data-act="zoom-out" title="缩小">−</button>',
      '    <button type="button" data-act="zoom-in" title="放大">＋</button>',
      '    <button type="button" data-act="reset" title="还原">1:1</button>',
      '  </div>',
      '  <div class="pm-actions"></div>',
      '</div>',
      '<button type="button" class="pm-nav pm-prev" title="上一张">‹</button>',
      '<button type="button" class="pm-nav pm-next" title="下一张">›</button>',
      '<button type="button" class="pm-close" title="关闭">×</button>',
      '<div class="pm-counter"></div>'
    ].join('');
    document.body.appendChild(root);

    this.el = root;
    this.img = root.querySelector('.pm-full');
    this.info = root.querySelector('.pm-info');
    this.actions = root.querySelector('.pm-actions');
    this.counter = root.querySelector('.pm-counter');
  };

  Lightbox.prototype.bind = function () {
    var self = this;

    this.el.querySelector('.pm-close').addEventListener('click', function () { self.close(); });
    this.el.querySelector('.pm-prev').addEventListener('click', function () { self.step(-1); });
    this.el.querySelector('.pm-next').addEventListener('click', function () { self.step(1); });
    /* 点图片本身、点图片外的空白、点背景都算关闭；但拖拽平移之后那一下不算 */
    this.el.addEventListener('click', function (event) {
      if (self.movedPx > 3) { self.movedPx = 0; return; }
      var target = event.target;
      if (
        target === self.el ||
        target === self.img ||
        (target.classList && target.classList.contains('pm-stage'))
      ) {
        self.close();
      }
    });

    this.el.querySelector('.pm-tools').addEventListener('click', function (event) {
      var act = event.target.getAttribute('data-act');
      if (act === 'zoom-in') self.zoom(1.25);
      if (act === 'zoom-out') self.zoom(0.8);
      if (act === 'reset') { self.scale = 1; self.panX = 0; self.panY = 0; self.apply(); }
    });

    this.el.addEventListener('wheel', function (event) {
      if (!self.el.classList.contains('pm-open')) return;
      event.preventDefault();
      self.zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });

    this.stage = this.el.querySelector('.pm-stage');
    var dragging = false;
    var lastX = 0;
    var lastY = 0;
    this.stage.addEventListener('mousedown', function (event) {
      if (self.scale <= 1) return;
      dragging = true;
      self.movedPx = 0;
      lastX = event.clientX;
      lastY = event.clientY;
      self.stage.classList.add('pm-dragging');
    });
    window.addEventListener('mousemove', function (event) {
      if (!dragging) return;
      self.movedPx += Math.abs(event.clientX - lastX) + Math.abs(event.clientY - lastY);
      self.panX += event.clientX - lastX;
      self.panY += event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;
      self.apply();
    });
    window.addEventListener('mouseup', function () {
      dragging = false;
      if (self.stage) self.stage.classList.remove('pm-dragging');
    });

    document.addEventListener('keydown', function (event) {
      if (!self.el.classList.contains('pm-open')) return;
      if (event.key === 'Escape') {
        if (event.__pmEscOwner && event.__pmEscOwner !== 'lightbox') return;
        self.close();
      }
      if (event.key === 'ArrowLeft') self.step(-1);
      if (event.key === 'ArrowRight') self.step(1);
      if (event.key === '+' || event.key === '=') self.zoom(1.25);
      if (event.key === '-') self.zoom(0.8);
    });
  };

  Lightbox.prototype.show = function (index) {
    if (!this.photos.length) return;
    this.index = (index + this.photos.length) % this.photos.length;
    var p = this.photos[this.index];

    this.img.src = p.full || p.thumb || '';
    this.img.alt = p.name || '';
    this.scale = 1;
    this.panX = 0;
    this.panY = 0;
    this.movedPx = 0;
    this.apply();

    this.info.innerHTML = this.infoHtml(p);
    this.actions.innerHTML = this.actionsHtml(p);
    this.counter.textContent = (this.index + 1) + ' / ' + this.photos.length;
    this.el.classList.add('pm-open');
  };

  Lightbox.prototype.infoHtml = function (p) {
    var rows = [];
    function row(label, value) {
      if (value === undefined || value === null || value === '') return;
      rows.push('<div class="pm-kv"><span>' + esc(label) + '</span><b>' + esc(value) + '</b></div>');
    }
    row('拍摄时间', p.taken_at);
    row('坐标 · WGS-84', coordText(p.wgs_lat, p.wgs_lon));
    if (p.wgs_lat !== undefined && (p.lat !== p.wgs_lat || p.lon !== p.wgs_lon)) {
      row('坐标 · ' + this.mapLabel, coordText(p.lat, p.lon));
    }
    row('海拔', p.altitude === null || p.altitude === undefined ? '' : Number(p.altitude).toFixed(1) + ' m');
    row('文件名', p.name);
    return rows.join('');
  };

  Lightbox.prototype.actionsHtml = function (p) {
    var html = '';
    if (p.original) {
      html += '<a class="pm-btn" href="' + esc(p.original) + '" target="_blank" rel="noopener">打开本地原图</a>';
    }
    return html;
  };

  Lightbox.prototype.step = function (delta) {
    this.show(this.index + delta);
  };

  Lightbox.prototype.zoom = function (factor) {
    var next = this.scale * factor;
    if (next < 1) next = 1;
    if (next > 6) next = 6;
    this.scale = next;
    if (this.scale === 1) {
      this.panX = 0;
      this.panY = 0;
    }
    this.apply();
  };

  Lightbox.prototype.apply = function () {
    this.img.style.transform =
      'translate(' + this.panX + 'px,' + this.panY + 'px) scale(' + this.scale + ')';
    this.img.classList.toggle('pm-zoomed', this.scale > 1);
  };

  Lightbox.prototype.close = function () {
    this.el.classList.remove('pm-open');
  };

  /* 打开全局图集（侧边栏点击、单独一簇的照片）。 */
  Lightbox.prototype.open = function (index) {
    this.photos = this.allPhotos;
    this.show(index);
  };

  /* 打开某一簇的子集，页码与左右切换都只在簇内进行。 */
  Lightbox.prototype.setPhotos = function (photos, index) {
    this.photos = (photos && photos.length) ? photos : this.allPhotos;
    this.show(index || 0);
  };

  /* 更新完整图集（服务端照片列表变化时调用）。
     若当前正在看完整图集就同步刷新；正在看某一簇的子集时不动它。 */
  Lightbox.prototype.setAllPhotos = function (photos) {
    var showingAll = this.photos === this.allPhotos;
    this.allPhotos = photos || [];
    if (!showingAll) return;
    this.photos = this.allPhotos;
    if (!this.allPhotos.length) { this.close(); return; }
    if (this.el.classList.contains('pm-open')) this.show(this.index);
  };

  /* 侧边栏列表：只放缩略图和文件名，不显示坐标与时间。 */
  /* onEdit 给列表项加一个次级入口（比如「改位置」），静态导出不用它。 */
  function renderList(container, photos, onOpen, onEdit) {
    container.innerHTML = '';
    photos.forEach(function (p, i) {
      var el = document.createElement('div');
      el.className = 'item';
      el.dataset.index = String(i);
      el.innerHTML =
        (p.thumb ? '<img src="' + esc(p.thumb) + '" alt="" />' : '<span class="noimg"></span>') +
        '<span class="nm">' + esc(p.name) + '</span>';
      el.addEventListener('click', function () {
        Array.prototype.forEach.call(container.children, function (child) {
          child.classList.toggle('active', child === el);
        });
        if (onOpen) onOpen(i);
      });
      if (onEdit) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'pm-edit';
        button.textContent = '改位置';
        button.title = '修改这张照片的位置';
        button.addEventListener('click', function (event) {
          event.stopPropagation();
          onEdit(i, p);
        });
        el.appendChild(button);
      }
      container.appendChild(el);
    });
  }

  function highlight(container, index) {
    Array.prototype.forEach.call(container.children, function (child) {
      child.classList.toggle('active', Number(child.dataset.index) === index);
    });
  }

  /* 不同的坐标点数量：全挤在一个点时按固定缩放级别展示，
     否则 fitBounds 会把地图缩到街道级别。 */
  function coordCount(photos) {
    var seen = {};
    (photos || []).forEach(function (p) {
      seen[num(p.lat, 6) + '|' + num(p.lon, 6)] = true;
    });
    return Object.keys(seen).length;
  }

  /* ---------------------------------------------------------------- 坐标换算
     与 photonmap/coords.py 是同一套算法、常量也逐字对齐。
     服务端一律给 WGS-84，要画到国内底图上时在这里换算一次。
     （以前 shell.html 里另有一份副本，两份实现迟早会漂移，收到这里统一。） */
  var GCJ_A = 6378245.0;
  var GCJ_EE = 0.00669342162296594323;
  var CN_LON = [72.004, 137.8347];
  var CN_LAT = [0.8293, 55.8271];

  function outOfChina(lat, lon) {
    return !(lon >= CN_LON[0] && lon <= CN_LON[1] && lat >= CN_LAT[0] && lat <= CN_LAT[1]);
  }

  function transformLat(x, y) {
    var ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return ret;
  }

  function transformLon(x, y) {
    var ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return ret;
  }

  function wgs84ToGcj02(lat, lon) {
    if (outOfChina(lat, lon)) return { lat: lat, lon: lon };
    var dLat = transformLat(lon - 105.0, lat - 35.0);
    var dLon = transformLon(lon - 105.0, lat - 35.0);
    var radLat = lat / 180.0 * Math.PI;
    var magic = 1 - GCJ_EE * Math.pow(Math.sin(radLat), 2);
    var sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtMagic) * Math.PI);
    dLon = (dLon * 180.0) / (GCJ_A / sqrtMagic * Math.cos(radLat) * Math.PI);
    return { lat: lat + dLat, lon: lon + dLon };
  }

  function gcj02ToWgs84(lat, lon) {
    if (outOfChina(lat, lon)) return { lat: lat, lon: lon };
    var wlat = lat, wlon = lon;
    for (var i = 0; i < 30; i++) {
      var g = wgs84ToGcj02(wlat, wlon);
      var dLat = g.lat - lat;
      var dLon = g.lon - lon;
      if (Math.abs(dLat) < 1e-9 && Math.abs(dLon) < 1e-9) break;
      wlat -= dLat;
      wlon -= dLon;
    }
    return { lat: wlat, lon: wlon };
  }

  function gcj02ToBd09(lat, lon) {
    if (outOfChina(lat, lon)) return { lat: lat, lon: lon };
    var z = Math.sqrt(lat * lat + lon * lon) + 0.00002 * Math.sin(lat * Math.PI * 3000.0 / 180.0);
    var theta = Math.atan2(lat, lon) + 0.000003 * Math.cos(lon * Math.PI * 3000.0 / 180.0);
    return { lat: z * Math.sin(theta) + 0.006, lon: z * Math.cos(theta) + 0.0065 };
  }

  function bd09ToGcj02(lat, lon) {
    if (outOfChina(lat, lon)) return { lat: lat, lon: lon };
    var x = lon - 0.0065;
    var y = lat - 0.006;
    var z = Math.sqrt(x * x + y * y) - 0.00002 * Math.sin(y * Math.PI * 3000.0 / 180.0);
    var theta = Math.atan2(y, x) - 0.000003 * Math.cos(x * Math.PI * 3000.0 / 180.0);
    return { lat: z * Math.sin(theta), lon: z * Math.cos(theta) };
  }

  /* 统一的换算入口。source 省略时按 WGS-84 解释。 */
  function convertCoord(lat, lon, target, source) {
    target = (target || 'wgs84').toLowerCase();
    source = (source || 'wgs84').toLowerCase();
    if (source === target) return { lat: lat, lon: lon };
    if (source === 'wgs84' && target === 'gcj02') return wgs84ToGcj02(lat, lon);
    if (source === 'gcj02' && target === 'wgs84') return gcj02ToWgs84(lat, lon);
    if (source === 'gcj02' && target === 'bd09') return gcj02ToBd09(lat, lon);
    if (source === 'bd09' && target === 'gcj02') return bd09ToGcj02(lat, lon);
    if (source === 'wgs84' && target === 'bd09') {
      var g2 = wgs84ToGcj02(lat, lon);
      return gcj02ToBd09(g2.lat, g2.lon);
    }
    if (source === 'bd09' && target === 'wgs84') {
      var g = bd09ToGcj02(lat, lon);
      return gcj02ToWgs84(g.lat, g.lon);
    }
    return { lat: lat, lon: lon };
  }

  global.PhotoMap = {
    esc: esc,
    num: num,
    coordText: coordText,
    clusterByPixel: clusterByPixel,
    ClusterView: ClusterView,
    PendingView: PendingView,
    Filmstrip: Filmstrip,
    Lightbox: Lightbox,
    renderList: renderList,
    highlight: highlight,
    coordCount: coordCount,
    coord: {
      LABELS: { wgs84: 'WGS-84', gcj02: 'GCJ-02', bd09: 'BD-09' },
      convert: convertCoord,
      wgs84ToGcj02: wgs84ToGcj02,
      gcj02ToWgs84: gcj02ToWgs84,
      gcj02ToBd09: gcj02ToBd09,
      bd09ToGcj02: bd09ToGcj02
    }
  };
})(window);
