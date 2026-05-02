/* physics.js — jump physics, AABB collision resolution, obby platform meshes */
(function () {
  'use strict';

  const Physics = {
    GRAVITY:        -18,
    JUMP_FORCE:       7.5,
    FLOOR_Y:          0,
    playerVY:         0,
    playerOnGround:   true,

    // Obby staircase [x, y_top, z, width, depth]
    OBBY_PLATFORMS: [
      [15,   1.0, -13,   3.5, 2.5],
      [16.5, 2.2, -15,   3.0, 2.0],
      [15,   3.4, -16.5, 3.0, 2.0],
      [13,   4.6, -15,   3.0, 2.0],
      [14,   5.8, -13,   3.0, 2.0],
      [16,   7.0, -14,   3.5, 2.5],
    ],

    // AABB boxes for static furniture — [minX, maxX, minZ, maxZ]
    // Agent positions: architect[-10,0,-10], coder[0,0,-10], debugger[10,0,-10],
    //                  tester[-5,0,-2], reviewer[5,0,-2]
    // Desk top geometry: BoxGeometry(2.6, 0.1, 1.3), monitor behind at z=-0.42
    STATIC_BOXES: [
      // Agent desks (padded half-size 1.4 x 1.0 + monitor depth)
      { minX: -11.5, maxX:  -8.5, minZ: -11.2, maxZ:  -9.2 }, // architect
      { minX:  -1.5, maxX:   1.5, minZ: -11.2, maxZ:  -9.2 }, // coder
      { minX:   8.5, maxX:  11.5, minZ: -11.2, maxZ:  -9.2 }, // debugger
      { minX:  -6.5, maxX:  -3.5, minZ:  -3.2, maxZ:  -1.2 }, // tester
      { minX:   3.5, maxX:   6.5, minZ:  -3.2, maxZ:  -1.2 }, // reviewer
      // Conference table (circle r=1.8 at -17,6 → square AABB)
      { minX: -19.2, maxX: -14.8, minZ:   4.0, maxZ:   8.0 },
      // Reception desk (4.5 x 0.8 at 0,16)
      { minX:  -2.8, maxX:   2.8, minZ:  15.4, maxZ:  17.0 },
      // Coffee machine (2.0 x 0.9 at -10,16)
      { minX: -11.2, maxX:  -8.8, minZ:  15.4, maxZ:  17.0 },
      // Basketball hoop pole (thin, at -14,0,-10)
      { minX: -14.4, maxX: -13.6, minZ: -10.4, maxZ:  -9.6 },
    ],

    /** Spawn obby platform meshes into the Three.js scene. */
    init(scene) {
      const platMat = new THREE.MeshLambertMaterial({ color: 0x6366f1 });
      const edgeMat = new THREE.MeshLambertMaterial({ color: 0x818cf8 });
      this.OBBY_PLATFORMS.forEach(([px, py, pz, pw, pd]) => {
        const body = new THREE.Mesh(new THREE.BoxGeometry(pw, 0.3, pd), platMat);
        body.position.set(px, py - 0.15, pz);
        scene.add(body);
        const edge = new THREE.Mesh(new THREE.BoxGeometry(pw, 0.06, pd), edgeMat);
        edge.position.set(px, py, pz);
        scene.add(edge);
      });
    },

    /**
     * Resolve player XZ position against all static AABB boxes and obby platform sides.
     * @param {number} x  candidate X
     * @param {number} z  candidate Z
     * @param {number} playerY  current player Y (used for platform side test)
     * @param {number} r  player capsule radius (default 0.38)
     * @returns {{ x: number, z: number }}
     */
    resolveStaticCollision(x, z, playerY, r) {
      r = r || 0.38;
      let cx = x, cz = z;

      // ── Furniture AABB boxes ─────────────────────────────
      for (const box of this.STATIC_BOXES) {
        const nearX = Math.max(box.minX, Math.min(box.maxX, cx));
        const nearZ = Math.max(box.minZ, Math.min(box.maxZ, cz));
        const dx = cx - nearX;
        const dz = cz - nearZ;
        const dist = Math.sqrt(dx * dx + dz * dz);
        if (dist < r) {
          if (dist < 0.0001) {
            // Exactly inside — pick shortest escape
            const dL = cx - box.minX, dR = box.maxX - cx;
            const dB = cz - box.minZ, dF = box.maxZ - cz;
            const m = Math.min(dL, dR, dB, dF);
            if      (m === dL) cx = box.minX - r;
            else if (m === dR) cx = box.maxX + r;
            else if (m === dB) cz = box.minZ - r;
            else               cz = box.maxZ + r;
          } else {
            const push = r - dist;
            cx += (dx / dist) * push;
            cz += (dz / dist) * push;
          }
        }
      }

      // ── Obby platform side walls ─────────────────────────
      // Only block sides when player is at the platform's slab height.
      // This lets the player jump up from below onto the platform.
      for (const [px, py, pz, pw, pd] of this.OBBY_PLATFORMS) {
        const platBase = py - 0.31;  // slab is 0.30 thick
        if (playerY < platBase || playerY > py + 0.15) continue;
        const halfW = pw / 2 + r;
        const halfD = pd / 2 + r;
        if (Math.abs(cx - px) < halfW && Math.abs(cz - pz) < halfD) {
          const overlapX = halfW - Math.abs(cx - px);
          const overlapZ = halfD - Math.abs(cz - pz);
          if (overlapX <= overlapZ) {
            cx += cx > px ? overlapX : -overlapX;
          } else {
            cz += cz > pz ? overlapZ : -overlapZ;
          }
        }
      }

      return { x: cx, z: cz };
    },

    /**
     * Solid-collision test: returns true if (x, z) overlaps any static furniture box.
     * Used by updatePlayer to block entry rather than push out.
     */
    wouldCollide(x, z, r) {
      r = r || 0.38;
      for (const box of this.STATIC_BOXES) {
        const nearX = Math.max(box.minX, Math.min(box.maxX, x));
        const nearZ = Math.max(box.minZ, Math.min(box.maxZ, z));
        const dx = x - nearX, dz = z - nearZ;
        if (dx * dx + dz * dz < r * r) return true;
      }
      return false;
    },

    /**
     * Update jump/gravity for the player. Call every frame.
     * @param {number} dt  delta time in seconds
     * @param {THREE.Group} playerGroup
     * @param {Object} keys  key state map
     * @param {Function} [playTone]
     */
    updateJump(dt, playerGroup, keys, playTone) {
      if (this.playerOnGround && !keys['Space']) return;

      this.playerVY += this.GRAVITY * dt;
      playerGroup.position.y += this.playerVY * dt;

      const px  = playerGroup.position.x;
      const pz  = playerGroup.position.z;
      const py  = playerGroup.position.y;
      let landed = false;

      for (const [platX, platY, platZ, pw, pd] of this.OBBY_PLATFORMS) {
        const halfW = pw / 2 + 0.25;
        const halfD = pd / 2 + 0.25;
        if (Math.abs(px - platX) < halfW && Math.abs(pz - platZ) < halfD) {
          if (py <= platY + 0.06 && py >= platY - 0.65 && this.playerVY <= 0) {
            playerGroup.position.y = platY;
            this.playerVY = 0;
            this.playerOnGround = true;
            landed = true;
            break;
          }
        }
      }

      if (!landed && playerGroup.position.y <= this.FLOOR_Y) {
        const wasAirborne = !this.playerOnGround;
        playerGroup.position.y = this.FLOOR_Y;
        this.playerVY = 0;
        this.playerOnGround = true;
        if (wasAirborne && playTone) {
          playTone(70, 0.06, { type: 'sine', volume: 0.07, endFreq: 50 });
        }
      }
    },
  };

  window.Physics = Physics;
})();
