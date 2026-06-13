import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { createWorld, simToThree } from './scene.js'

const API = import.meta.env.VITE_SIM_API || 'http://127.0.0.1:8200'

const STATE_POLL_MS = 100
const FRAME_PUSH_MS = 180

const els = {
  scene: document.getElementById('scene'),
  fpv: document.getElementById('fpv'),
  connDot: document.getElementById('conn-dot'),
  tConn: document.getElementById('t-conn'),
  tAir: document.getElementById('t-air'),
  tBat: document.getElementById('t-bat'),
  tHeight: document.getElementById('t-height'),
  tYaw: document.getElementById('t-yaw'),
  tLast: document.getElementById('t-last'),
  events: document.getElementById('events'),
}

// Authoritative target pose from the service; the rendered drone eases toward it.
const target = { pos: new THREE.Vector3(), yaw: 0, hasPose: false }

async function boot() {
  let layout
  try {
    layout = await fetch(`${API}/api/sim/layout`).then((r) => r.json())
  } catch (err) {
    els.tConn.textContent = 'service offline'
    console.error('Could not load layout from sim service:', err)
    return
  }

  const world = createWorld(layout)
  const start = layout.start || { x: 0, y: 0, z: 0, yaw: 0 }
  target.pos.copy(simToThree(start.x, start.y, start.z))
  target.yaw = (start.yaw || 0) * (Math.PI / 180)
  world.drone.position.copy(target.pos)
  world.drone.rotation.y = -target.yaw

  // Main renderer (third-person).
  const renderer = new THREE.WebGLRenderer({ canvas: els.scene, antialias: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setSize(window.innerWidth, window.innerHeight)

  // FPV renderer. preserveDrawingBuffer lets us read pixels for the vision feed.
  const fpvRenderer = new THREE.WebGLRenderer({
    canvas: els.fpv,
    antialias: true,
    preserveDrawingBuffer: true,
  })
  fpvRenderer.setPixelRatio(1)
  fpvRenderer.setSize(els.fpv.clientWidth, els.fpv.clientHeight, false)

  const controls = new OrbitControls(world.thirdPersonCamera, renderer.domElement)
  controls.enableDamping = true
  controls.target.copy(target.pos)

  window.addEventListener('resize', () => {
    world.thirdPersonCamera.aspect = window.innerWidth / window.innerHeight
    world.thirdPersonCamera.updateProjectionMatrix()
    renderer.setSize(window.innerWidth, window.innerHeight)
  })

  startStatePolling()
  startEventStream()
  startFramePush(fpvRenderer, els.fpv)

  const clock = new THREE.Clock()
  function animate() {
    requestAnimationFrame(animate)
    const dt = clock.getDelta()

    // Spin props for liveliness.
    for (const prop of world.drone.userData.props) {
      prop.rotation.z += dt * 30
    }

    // Smoothly ease the drone toward the authoritative pose.
    if (target.hasPose) {
      world.drone.position.lerp(target.pos, Math.min(1, dt * 8))
      const targetRotY = -target.yaw
      world.drone.rotation.y += angleDelta(world.drone.rotation.y, targetRotY) * Math.min(1, dt * 8)
    }

    controls.target.lerp(world.drone.position, Math.min(1, dt * 4))
    controls.update()

    renderer.render(world.scene, world.thirdPersonCamera)
    fpvRenderer.render(world.scene, world.fpvCamera)
  }
  animate()
}

function angleDelta(from, to) {
  let d = (to - from) % (Math.PI * 2)
  if (d > Math.PI) d -= Math.PI * 2
  if (d < -Math.PI) d += Math.PI * 2
  return d
}

function startStatePolling() {
  const tick = async () => {
    try {
      const data = await fetch(`${API}/api/sim/state`).then((r) => r.json())
      applyState(data)
      setConnected(true)
    } catch (err) {
      setConnected(false)
    }
  }
  tick()
  setInterval(tick, STATE_POLL_MS)
}

function applyState(data) {
  const pose = data.pose || {}
  target.pos.copy(simToThree(pose.x || 0, pose.y || 0, pose.z || 0))
  target.yaw = (pose.yaw || 0) * (Math.PI / 180)
  target.hasPose = true

  const drone = data.drone || {}
  els.tAir.textContent = drone.airborne ? 'yes' : 'no'
  els.tBat.textContent = drone.battery_percent != null ? `${drone.battery_percent}%` : '--'
  els.tHeight.textContent = drone.height_cm != null ? `${drone.height_cm} cm` : '--'
  els.tYaw.textContent = pose.yaw != null ? `${Math.round(pose.yaw)}\u00b0` : '--'
  els.tLast.textContent = data.last_command || 'none'
}

function setConnected(online) {
  els.connDot.classList.toggle('online', online)
  els.tConn.textContent = online ? 'online' : 'service offline'
}

function startEventStream() {
  try {
    const source = new EventSource(`${API}/events`)
    source.onmessage = (msg) => {
      try {
        addEvent(JSON.parse(msg.data))
      } catch {
        /* ignore malformed lines */
      }
    }
    source.onerror = () => {
      /* EventSource auto-reconnects */
    }
  } catch (err) {
    console.error('Event stream failed:', err)
  }
}

function addEvent(event) {
  const placeholder = els.events.querySelector('.muted')
  if (placeholder) placeholder.remove()

  const title = event.type || event.event || event.role || 'event'
  const body =
    event.text ||
    event.message ||
    event.transcript ||
    event.spoken_message ||
    (event.command ? `command: ${event.command}` : '') ||
    JSON.stringify(event)

  const node = document.createElement('div')
  node.className = 'event'
  node.innerHTML = `<div class="title"></div><p class="body"></p>`
  node.querySelector('.title').textContent = String(title)
  node.querySelector('.body').textContent = String(body)
  els.events.prepend(node)

  while (els.events.children.length > 40) {
    els.events.removeChild(els.events.lastChild)
  }
}

function startFramePush(fpvRenderer, fpvCanvas) {
  const push = async () => {
    let dataUrl
    try {
      dataUrl = fpvCanvas.toDataURL('image/jpeg', 0.6)
    } catch (err) {
      return
    }
    const base64 = dataUrl.split(',', 2)[1]
    if (!base64) return
    try {
      await fetch(`${API}/api/sim/frame`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_base64: base64 }),
      })
    } catch {
      /* service may be briefly unavailable */
    }
  }
  setInterval(push, FRAME_PUSH_MS)
}

boot()
