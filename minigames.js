/* minigames.js — Coin Dash, Basketball (drag-to-throw), Target Dash */
(function () {
  'use strict';

  // ── shared refs set by init() ────────────────────────
  let _scene, _player, _renderer, _physics, _keys;
  let _playTone, _showToast;

  // ── Basketball geometry (shared) ─────────────────────
  const _bbBallMat  = new THREE.MeshLambertMaterial({ color: 0xf97316 });
  const _bbBallGeom = new THREE.SphereGeometry(0.18, 8, 6);

  // ── Target Dash geometry ─────────────────────────────
  const _tdGeom = new THREE.CylinderGeometry(0.45, 0.45, 0.12, 16);

  // ── Hoop world-position ──────────────────────────────
  const HOOP_POS         = new THREE.Vector3(-14, 3.8, -10);
  const HOOP_SHOOT_RANGE = 12;   // max distance from hoop to shoot

  // ── Ping-pong table world-position ───────────────────
  const PP_POS   = new THREE.Vector3(10, 0, 10);
  const PP_RANGE = 5;

  // ── Coin spawn spots ─────────────────────────────────
  const COIN_SPOTS = [
    [ 0,   0.35,  0],  [4,  0.35,  4], [-4,  0.35,  3],
    [ 6,   0.35, -4],  [-6, 0.35, -3], [ 3,  0.35, -8],
    [-5,   0.35,  8],  [ 8, 0.35,  6], [-8,  0.35, -6],
    [ 0,   0.35,-10],  [10, 0.35,  2], [-10, 0.35,  3],
    // platform coins
    [15,  1.5, -13], [16.5, 2.7, -15], [15, 3.9, -16.5],
    [13,  5.1, -15], [14,  6.3, -13],  [16, 7.5, -14],
  ];

  // ── Target Dash spots ────────────────────────────────
  const TD_SPOTS = [
    [ 4, 0.12,  6], [-4, 0.12,  6], [ 8, 0.12, 0], [-8, 0.12,  0],
    [ 4, 0.12, -6], [-4, 0.12, -6], [ 0, 0.12, 10], [ 0, 0.12,-10],
    [ 6, 0.12, 10], [-6, 0.12, 10],
  ];

  // ── Public MiniGames object ──────────────────────────
  const MG = {
    active: false,
    score:  0,

    // internal state
    _coins:      [],
    _coinMat:    null,
    _coinGeom:   null,
    _hoopGroup:  null,
    _ball:       null,   // single ball { mesh, vel, scored, age }

    _heldBall:   null,   // always-visible ball at player's hand

    _bbDragging: false,
    _bbDragStart: null,
    _bbPowerEl:  null,   // power-meter DOM element
    _bbFired:    false,  // debounce double-fire

    // Ping-pong
    _ppActive:   false,
    _ppOverlay:  null,
    _ppCanvas:   null,
    _ppCtx:      null,
    _ppBall:     { x: 0.5, y: 0.5, vx: 0, vy: 0 },
    _ppPlayerY:  0.5,
    _ppAiY:      0.5,
    _ppScoreP:   0,
    _ppScoreAI:  0,

    _targets:     [],
    _tdMat:       null,
    _tdMatLit:    null,
    _tdActive:    false,
    _tdLitIndex: -1,
    _tdNextLitAt: 0,
    _tdHitCount:  0,
  };

  // ────────────────────────────────────────────────────
  //  Init
  // ────────────────────────────────────────────────────
  MG.init = function (ctx) {
    _scene    = ctx.scene;
    _player   = ctx.player;
    _renderer = ctx.renderer;
    _physics  = ctx.physics;
    _keys     = ctx.keys;
    _playTone = ctx.playTone;
    _showToast = ctx.showToast;

    MG._coinMat  = new THREE.MeshLambertMaterial({ color: 0xfbbf24, emissive: 0xf59e0b, emissiveIntensity: 0.5 });
    MG._coinGeom = new THREE.CylinderGeometry(0.22, 0.22, 0.08, 12);
    MG._tdMat    = new THREE.MeshLambertMaterial({ color: 0x22c55e, emissive: 0x16a34a });
    MG._tdMatLit = new THREE.MeshLambertMaterial({ color: 0xfbbf24, emissive: 0xf59e0b });
    MG._bbPowerEl = document.getElementById('bb-power-overlay');

    _buildHoop();
    _buildPingPongTable();

    // Held basketball — always in scene at player's hand
    MG._heldBall = new THREE.Mesh(_bbBallGeom, new THREE.MeshLambertMaterial({ color: 0xf97316 }));
    MG._heldBall.visible = true;
    _scene.add(MG._heldBall);

    // Basketball drag events
    _renderer.domElement.addEventListener('mousedown', _bbMouseDown);
    window.addEventListener('mousemove',  _bbMouseMove);
    window.addEventListener('mouseup',    _bbMouseUp);
  };

  MG.start = function () {
    if (MG.active) return;
    MG.active = true;
    MG.score  = 0;
    document.getElementById('mg-score').textContent = '0';
    document.getElementById('mg-title').textContent = 'COINS + GAMES';
    document.getElementById('mg-hint').textContent  = 'SPACE jump  •  drag near hoop';
    document.getElementById('minigame-hud').classList.add('visible');
    _spawnCoins(12);
    _spawnTargets();
    _showToast('Games unlocked! Collect coins, drag to shoot hoops, dash to targets!', 'working');
  };

  MG.stop = function () {
    if (!MG.active) return;
    MG.active = false;
    document.getElementById('minigame-hud').classList.remove('visible');
    _despawnCoins();
    _clearBall();
    _clearTargets();
    _hidePower();
    MG._bbDragging = false;
    MG._bbDragStart = null;
  };

  /** True when player is close enough to the ping-pong table. */
  MG.nearPingPong = function () {
    const dx = _player.group.position.x - PP_POS.x;
    const dz = _player.group.position.z - PP_POS.z;
    return Math.sqrt(dx * dx + dz * dz) < PP_RANGE;
  };

  MG.startPingPong = function () { _startPingPong(); };
  MG.stopPingPong  = function () { _stopPingPong(); };

  /** Call from the main animate() loop every frame. */
  MG.tick = function (dt, t) {
    _tickHeldBall();
    _tickCoins(t);
    _tickBall(dt);
    _tickTargets(t);
    _tickPingPong(dt);
  };

  /** True when player is close enough to the hoop to shoot. */
  MG.nearHoop = function () {
    const dx = _player.group.position.x - HOOP_POS.x;
    const dz = _player.group.position.z - HOOP_POS.z;
    return Math.sqrt(dx * dx + dz * dz) < HOOP_SHOOT_RANGE;
  };

  // ────────────────────────────────────────────────────
  //  Coin Dash
  // ────────────────────────────────────────────────────
  function _spawnCoins(count) {
    _despawnCoins();
    const spots = [...COIN_SPOTS].sort(() => Math.random() - 0.5).slice(0, count);
    spots.forEach(([cx, cy, cz]) => {
      const mesh = new THREE.Mesh(MG._coinGeom, MG._coinMat.clone());
      mesh.position.set(cx, cy, cz);
      mesh.rotation.x = Math.PI / 2;
      _scene.add(mesh);
      MG._coins.push({ mesh, x: cx, y: cy, z: cz, collected: false });
    });
  }

  function _despawnCoins() {
    MG._coins.forEach(c => _scene.remove(c.mesh));
    MG._coins.length = 0;
  }

  function _tickCoins(t) {
    if (!MG.active) return;
    const px = _player.group.position.x;
    const py = _player.group.position.y;
    const pz = _player.group.position.z;
    MG._coins.forEach(c => {
      if (c.collected) return;
      c.mesh.rotation.z = t * 2.5;
      c.mesh.position.y = c.y + Math.sin(t * 2 + c.x) * 0.12;
      const dx = px - c.x, dy = py - c.y, dz = pz - c.z;
      if (Math.sqrt(dx * dx + dy * dy * 0.3 + dz * dz) < 0.9) {
        c.collected = true;
        _scene.remove(c.mesh);
        MG.score++;
        document.getElementById('mg-score').textContent = MG.score;
        _playTone(880, 0.07, { type: 'sine', volume: 0.12, endFreq: 1100 });
        setTimeout(() => _playTone(1100, 0.05, { type: 'sine', volume: 0.08 }), 60);
        _pop('+1 \u2b50', 0);
        if (MG._coins.filter(c2 => !c2.collected).length === 0) {
          setTimeout(() => _spawnCoins(14), 1500);
        }
      }
    });
  }

  // ────────────────────────────────────────────────────
  //  Basketball
  // ────────────────────────────────────────────────────
  function _buildHoop() {
    if (MG._hoopGroup) _scene.remove(MG._hoopGroup);
    MG._hoopGroup = new THREE.Group();
    // backboard
    const board = new THREE.Mesh(
      new THREE.BoxGeometry(2.2, 1.4, 0.08),
      new THREE.MeshLambertMaterial({ color: 0xdbeafe })
    );
    MG._hoopGroup.add(board);
    // rim
    const rim = new THREE.Mesh(
      new THREE.TorusGeometry(0.38, 0.045, 8, 20),
      new THREE.MeshLambertMaterial({ color: 0xef4444 })
    );
    rim.rotation.x = Math.PI / 2;
    rim.position.set(0, -0.35, 0.42);
    MG._hoopGroup.add(rim);
    // pole
    const pole = new THREE.Mesh(
      new THREE.CylinderGeometry(0.06, 0.06, 4.5, 8),
      new THREE.MeshLambertMaterial({ color: 0x94a3b8 })
    );
    pole.position.set(0, -3.1, 0);
    MG._hoopGroup.add(pole);
    MG._hoopGroup.position.copy(HOOP_POS);
    _scene.add(MG._hoopGroup);
  }

  // ── Ping-pong table scene mesh ────────────────────────
  function _buildPingPongTable() {
    const g = new THREE.Group();
    const tableMat = new THREE.MeshLambertMaterial({ color: 0x166534 });
    const top = new THREE.Mesh(new THREE.BoxGeometry(2.6, 0.07, 1.5), tableMat);
    top.position.y = 0.82; g.add(top);
    const legMat = new THREE.MeshLambertMaterial({ color: 0x374151 });
    for (const [lx, lz] of [[-1.15, -0.6], [-1.15, 0.6], [1.15, -0.6], [1.15, 0.6]]) {
      const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.82, 8), legMat);
      leg.position.set(lx, 0.41, lz); g.add(leg);
    }
    const netMat = new THREE.MeshLambertMaterial({ color: 0xf1f5f9, transparent: true, opacity: 0.7 });
    const net = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.15, 1.5), netMat);
    net.position.set(0, 0.935, 0); g.add(net);
    const lineMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    const cLine = new THREE.Mesh(new THREE.BoxGeometry(2.6, 0.005, 0.018), lineMat);
    cLine.position.set(0, 0.856, 0); g.add(cLine);
    const glow = new THREE.PointLight(0x22c55e, 0.6, 8);
    glow.position.set(0, 2.2, 0); g.add(glow);
    g.position.copy(PP_POS);
    _scene.add(g);
  }

  // ── Held basketball follows player's hand ────────────
  function _tickHeldBall() {
    if (!MG._heldBall) return;
    if (MG._ball) { MG._heldBall.visible = false; return; }
    MG._heldBall.visible = true;
    const face = _player.group.rotation.y;
    const rightX =  Math.cos(face);
    const rightZ = -Math.sin(face);
    MG._heldBall.position.set(
      _player.group.position.x + rightX * 0.42,
      _player.group.position.y + 1.25,
      _player.group.position.z + rightZ * 0.42
    );
  }

  function _clearBall() {
    if (MG._ball) { _scene.remove(MG._ball.mesh); MG._ball = null; }
  }

  /**
   * Shoot the ball.
   * @param {number} powerPct  0–1
   * @param {number} aimDX     lateral aim offset in world units
   */
  function _shootBall(powerPct, aimDX) {
    if (!MG.active) return;
    _clearBall(); // enforce single-ball rule

    // State bonuses
    const isRunning = _keys['ShiftLeft'] || _keys['ShiftRight'];
    const isJumping = !_physics.playerOnGround;
    const bonus     = isJumping ? 1.20 : isRunning ? 1.12 : 1.0;

    const start = new THREE.Vector3(
      _player.group.position.x,
      _player.group.position.y + 1.6,
      _player.group.position.z
    );
    const target = new THREE.Vector3(
      HOOP_POS.x + aimDX,
      HOOP_POS.y + 0.5,
      HOOP_POS.z
    );
    const toHoop = new THREE.Vector3().subVectors(target, start);
    const dist   = toHoop.length();
    toHoop.normalize();

    // power range: 0.45× (flop) → 1.35× (overshoot) of the ideal arc
    const power = (0.45 + powerPct * 0.9) * bonus;
    const vel   = toHoop.clone().multiplyScalar(dist * 0.9 * power);
    vel.y      += dist * 0.55 * power;

    const mesh = new THREE.Mesh(_bbBallGeom, _bbBallMat.clone());
    mesh.position.copy(start);
    _scene.add(mesh);
    MG._ball = { mesh, vel, scored: false, age: 0 };

    _playTone(300, 0.05, { type: 'triangle', volume: 0.09, endFreq: 380 });
  }

  function _tickBall(dt) {
    if (!MG._ball) return;
    const b = MG._ball;
    b.age += dt;
    b.vel.y += _physics.GRAVITY * dt;
    b.mesh.position.addScaledVector(b.vel, dt);
    b.mesh.rotation.z += dt * b.vel.length() * 1.5; // rolling spin

    if (!b.scored) {
      const rim = new THREE.Vector3(HOOP_POS.x, HOOP_POS.y - 0.3, HOOP_POS.z);
      if (b.mesh.position.distanceTo(rim) < 0.52) {
        b.scored = true;
        MG.score += 3;
        document.getElementById('mg-score').textContent = MG.score;
        _playTone(660, 0.08, { type: 'sine', volume: 0.12, endFreq: 880 });
        setTimeout(() => _playTone(880, 0.07, { type: 'sine', volume: 0.09 }), 70);
        _showToast('\uD83C\uDF51 Basket! +3', 'complete');
        _pop('+3 \uD83C\uDF51', 0);
      }
    }

    if (b.age > 5 || b.mesh.position.y < -3) {
      _scene.remove(b.mesh);
      MG._ball = null;
    }
  }

  // ── Drag-to-throw input ──────────────────────────────
  function _bbMouseDown(e) {
    if (e.button !== 0) return;
    if (!MG.active || !MG.nearHoop()) return;
    MG._bbDragging = true;
    MG._bbFired    = false;
    MG._bbDragStart = { x: e.clientX, y: e.clientY };
    _showPower(0, e.clientX, e.clientY);
    e.stopPropagation(); // don't also start camera drag
  }

  function _bbMouseMove(e) {
    if (!MG._bbDragging || !MG._bbDragStart) return;
    const dx   = e.clientX - MG._bbDragStart.x;
    const dy   = e.clientY - MG._bbDragStart.y;
    const dist = Math.sqrt(dx * dx + dy * dy);
    const pct  = Math.min(1, dist / 200); // 200px = full power
    MG._bbPowerPct = pct;
    _showPower(pct, MG._bbDragStart.x, MG._bbDragStart.y);
  }

  function _bbMouseUp(e) {
    if (!MG._bbDragging || !MG._bbDragStart || MG._bbFired) return;
    const dx   = e.clientX - MG._bbDragStart.x;
    const dy   = e.clientY - MG._bbDragStart.y;
    const dist = Math.sqrt(dx * dx + dy * dy);
    MG._bbDragging = false;
    MG._bbFired    = true;
    _hidePower();

    if (dist < 12) { MG._bbDragStart = null; return; } // micro-drag = no shoot

    // Lateral aim: dragging left → ball drifts left of hoop
    const aimDX = -(dx / 200) * 2.0;
    _shootBall(MG._bbPowerPct, aimDX);
    MG._bbDragStart = null;
  }

  function _showPower(pct, sx, sy) {
    const el = MG._bbPowerEl;
    if (!el) return;
    el.style.display = 'flex';
    el.style.left    = (sx - 64) + 'px';
    el.style.top     = (sy - 110) + 'px';
    const fill  = el.querySelector('.bb-power-fill');
    const label = el.querySelector('.bb-power-label');
    if (fill) {
      fill.style.width      = (pct * 100) + '%';
      fill.style.background = pct < 0.38 ? '#22c55e' : pct < 0.72 ? '#f59e0b' : '#ef4444';
    }
    if (label) {
      const state = !_physics.playerOnGround ? ' (air +20%)' : (_keys['ShiftLeft'] || _keys['ShiftRight']) ? ' (run +12%)' : '';
      label.textContent = Math.round(pct * 100) + '% power' + state;
    }
  }

  function _hidePower() {
    if (MG._bbPowerEl) MG._bbPowerEl.style.display = 'none';
  }

  // ────────────────────────────────────────────────────
  //  Ping-pong
  // ────────────────────────────────────────────────────
  function _startPingPong() {
    if (MG._ppActive) return;
    MG._ppActive  = true;
    MG._ppScoreP  = 0;
    MG._ppScoreAI = 0;
    MG._ppPlayerY = 0.5;
    MG._ppAiY     = 0.5;

    MG._ppOverlay = document.createElement('div');
    MG._ppOverlay.style.cssText =
      'position:fixed;inset:0;background:rgba(0,0,0,.88);display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;z-index:999;';

    const title = document.createElement('div');
    title.textContent = 'PING PONG';
    title.style.cssText = 'color:#f1f5f9;font-size:22px;font-weight:700;margin-bottom:8px;font-family:monospace;letter-spacing:4px;';
    MG._ppOverlay.appendChild(title);

    const scoreEl = document.createElement('div');
    scoreEl.id = 'pp-score';
    scoreEl.style.cssText = 'color:#94a3b8;font-size:15px;margin-bottom:10px;font-family:monospace;';
    scoreEl.textContent = 'You  0 — 0  AI';
    MG._ppOverlay.appendChild(scoreEl);

    MG._ppCanvas = document.createElement('canvas');
    MG._ppCanvas.width  = 620;
    MG._ppCanvas.height = 380;
    MG._ppCanvas.style.cssText = 'border:2px solid #22c55e;border-radius:4px;cursor:none;display:block;';
    MG._ppCtx = MG._ppCanvas.getContext('2d');
    MG._ppOverlay.appendChild(MG._ppCanvas);

    const hint = document.createElement('div');
    hint.textContent = 'Move mouse to control paddle  •  Esc to quit';
    hint.style.cssText = 'color:#475569;font-size:11px;margin-top:8px;font-family:monospace;';
    MG._ppOverlay.appendChild(hint);

    document.body.appendChild(MG._ppOverlay);
    MG._ppOverlay.addEventListener('mousemove', _ppOnMouseMove);
    document.addEventListener('keydown', _ppOnKeyDown);
    _ppResetBall(1);
  }

  function _stopPingPong() {
    if (!MG._ppActive) return;
    MG._ppActive = false;
    document.removeEventListener('keydown', _ppOnKeyDown);
    if (MG._ppOverlay) { MG._ppOverlay.remove(); MG._ppOverlay = null; }
    MG._ppCanvas = null;
    MG._ppCtx = null;
  }

  function _ppOnMouseMove(e) {
    if (!MG._ppCanvas) return;
    const rect = MG._ppCanvas.getBoundingClientRect();
    MG._ppPlayerY = Math.max(0.12, Math.min(0.88, (e.clientY - rect.top) / MG._ppCanvas.height));
  }

  function _ppOnKeyDown(e) {
    if (e.key === 'Escape') _stopPingPong();
  }

  function _ppResetBall(dir) {
    const speed = 0.0045 + Math.random() * 0.002;
    MG._ppBall = { x: 0.5, y: 0.5, vx: speed * dir, vy: (Math.random() - 0.5) * 0.006 };
  }

  function _ppUpdateScore() {
    const el = document.getElementById('pp-score');
    if (el) el.textContent = `You  ${MG._ppScoreP} — ${MG._ppScoreAI}  AI`;
  }

  function _tickPingPong(dt) {
    if (!MG._ppActive || !MG._ppCtx) return;
    const W = MG._ppCanvas.width, H = MG._ppCanvas.height;
    const paddleW = 14, paddleH = H * 0.22;
    const ballR = 9;

    // AI tracks ball with limited speed
    const aiSpeed = 0.009 * dt * 60;
    if (MG._ppAiY < MG._ppBall.y - 0.04) MG._ppAiY = Math.min(0.88, MG._ppAiY + aiSpeed);
    if (MG._ppAiY > MG._ppBall.y + 0.04) MG._ppAiY = Math.max(0.12, MG._ppAiY - aiSpeed);

    // Move ball
    MG._ppBall.x += MG._ppBall.vx * dt * 60;
    MG._ppBall.y += MG._ppBall.vy * dt * 60;

    // Top/bottom bounce
    if (MG._ppBall.y < ballR / H) {
      MG._ppBall.vy = Math.abs(MG._ppBall.vy);
      MG._ppBall.y  = ballR / H;
      _playTone(440, 0.03, { type: 'sine', volume: 0.04 });
    }
    if (MG._ppBall.y > 1 - ballR / H) {
      MG._ppBall.vy = -Math.abs(MG._ppBall.vy);
      MG._ppBall.y  = 1 - ballR / H;
      _playTone(440, 0.03, { type: 'sine', volume: 0.04 });
    }

    // Player paddle (left)
    const playerPX = (paddleW * 2) / W;
    if (MG._ppBall.x < playerPX && MG._ppBall.vx < 0) {
      const relY = (MG._ppBall.y - MG._ppPlayerY) / 0.18;
      if (Math.abs(relY) < 1) {
        MG._ppBall.vx  = Math.abs(MG._ppBall.vx) * 1.06;
        MG._ppBall.vy  = relY * 0.011;
        MG._ppBall.x   = playerPX;
        _playTone(620, 0.05, { type: 'triangle', volume: 0.1 });
      }
    }

    // AI paddle (right)
    const aiPX = 1 - (paddleW * 2) / W;
    if (MG._ppBall.x > aiPX && MG._ppBall.vx > 0) {
      const relY = (MG._ppBall.y - MG._ppAiY) / 0.18;
      if (Math.abs(relY) < 1) {
        MG._ppBall.vx  = -Math.abs(MG._ppBall.vx) * 1.06;
        MG._ppBall.vy  = relY * 0.011;
        MG._ppBall.x   = aiPX;
        _playTone(500, 0.05, { type: 'triangle', volume: 0.07 });
      }
    }

    // Scoring
    if (MG._ppBall.x < 0) {
      MG._ppScoreAI++;
      _ppUpdateScore();
      _playTone(220, 0.14, { type: 'sawtooth', volume: 0.08, endFreq: 100 });
      if (MG._ppScoreAI < 7) _ppResetBall(1);
    }
    if (MG._ppBall.x > 1) {
      MG._ppScoreP++;
      _ppUpdateScore();
      _playTone(660, 0.1, { type: 'sine', volume: 0.09, endFreq: 880 });
      MG.score++;
      document.getElementById('mg-score').textContent = MG.score;
      if (MG._ppScoreP < 7) _ppResetBall(-1);
    }
    if (MG._ppScoreP >= 7 || MG._ppScoreAI >= 7) {
      const won = MG._ppScoreP > MG._ppScoreAI;
      _showToast(won ? 'You win the ping pong! +5' : 'AI wins ping pong...', won ? 'complete' : 'error');
      if (won) { MG.score += 5; document.getElementById('mg-score').textContent = MG.score; }
      _stopPingPong();
      return;
    }

    // Draw table
    const ctx = MG._ppCtx;
    ctx.fillStyle = '#15803d';
    ctx.fillRect(0, 0, W, H);

    // Side lines
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.lineWidth = 3;
    ctx.strokeRect(0, 0, W, H);

    // Center net
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    ctx.fillRect(W / 2 - 4, 0, 8, H);

    // Center horizontal line
    ctx.fillStyle = 'rgba(255,255,255,0.25)';
    ctx.fillRect(0, H / 2 - 2, W, 3);

    // Player paddle (blue)
    ctx.fillStyle = '#3b82f6';
    ctx.beginPath();
    ctx.roundRect(paddleW, MG._ppPlayerY * H - paddleH / 2, paddleW, paddleH, 4);
    ctx.fill();

    // AI paddle (red)
    ctx.fillStyle = '#ef4444';
    ctx.beginPath();
    ctx.roundRect(W - paddleW * 2, MG._ppAiY * H - paddleH / 2, paddleW, paddleH, 4);
    ctx.fill();

    // Ball
    ctx.fillStyle = '#fbbf24';
    ctx.shadowColor = '#fbbf24';
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.arc(MG._ppBall.x * W, MG._ppBall.y * H, ballR, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  // ────────────────────────────────────────────────────
  //  Target Dash
  // ────────────────────────────────────────────────────
  function _spawnTargets() {
    _clearTargets();
    TD_SPOTS.forEach(([tx, ty, tz]) => {
      const mesh = new THREE.Mesh(_tdGeom, MG._tdMat.clone());
      mesh.position.set(tx, ty, tz);
      _scene.add(mesh);
      MG._targets.push({ mesh, x: tx, z: tz, lit: false });
    });
    MG._tdActive    = true;
    MG._tdHitCount  = 0;
    MG._tdLitIndex  = -1;
    MG._tdNextLitAt = 0;
  }

  function _clearTargets() {
    MG._targets.forEach(t => _scene.remove(t.mesh));
    MG._targets.length = 0;
    MG._tdActive = false;
  }

  function _tickTargets(t) {
    if (!MG.active || !MG._tdActive || MG._targets.length === 0) return;

    if (t >= MG._tdNextLitAt) {
      if (MG._tdLitIndex >= 0) {
        MG._targets[MG._tdLitIndex].mesh.material = MG._tdMat.clone();
        MG._targets[MG._tdLitIndex].lit = false;
      }
      MG._tdLitIndex = Math.floor(Math.random() * MG._targets.length);
      MG._targets[MG._tdLitIndex].mesh.material = MG._tdMatLit.clone();
      MG._targets[MG._tdLitIndex].lit = true;
      MG._tdNextLitAt = t + 3.5;
      _playTone(440, 0.04, { type: 'sine', volume: 0.06, endFreq: 520 });
    }

    if (MG._tdLitIndex >= 0) {
      const tgt = MG._targets[MG._tdLitIndex];
      if (tgt.lit) {
        const dx = _player.group.position.x - tgt.x;
        const dz = _player.group.position.z - tgt.z;
        if (Math.sqrt(dx * dx + dz * dz) < 0.65 && _player.group.position.y < 0.5) {
          tgt.mesh.material = MG._tdMat.clone();
          tgt.lit           = false;
          MG._tdLitIndex    = -1;
          MG._tdNextLitAt   = t + 0.3;
          MG._tdHitCount++;
          MG.score += 2;
          document.getElementById('mg-score').textContent = MG.score;
          _playTone(520, 0.06, { type: 'sine', volume: 0.11, endFreq: 700 });
          setTimeout(() => _playTone(700, 0.05, { type: 'sine', volume: 0.08 }), 60);
          _pop('+2 \uD83C\uDFAF', 0);
        }
      }
    }
  }

  // ────────────────────────────────────────────────────
  //  Helpers
  // ────────────────────────────────────────────────────
  function _pop(text, offsetX) {
    const el = document.createElement('div');
    el.className  = 'coin-collect-pop';
    el.textContent = text;
    el.style.left  = (window.innerWidth / 2 + (Math.random() - 0.5) * 120 + (offsetX || 0)) + 'px';
    el.style.top   = (window.innerHeight * 0.44) + 'px';
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 900);
  }

  window.MiniGames = MG;
})();
