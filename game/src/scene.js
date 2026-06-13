import * as THREE from 'three'

// World scale: 1 cm in the simulator -> 0.01 three.js units (meters).
export const S = 0.01

// Coordinate mapping from simulator space to three.js space:
//   sim.x (right)        -> three.x
//   sim.z (up)           -> three.y
//   sim.y (forward/north)-> -three.z
export function simToThree(x, y, z) {
  return new THREE.Vector3(x * S, z * S, -y * S)
}

function makeDrone() {
  const group = new THREE.Group()

  const body = new THREE.Mesh(
    new THREE.BoxGeometry(0.14, 0.045, 0.14),
    new THREE.MeshStandardMaterial({ color: 0x1b2233, metalness: 0.4, roughness: 0.5 }),
  )
  group.add(body)

  const arm = new THREE.Mesh(
    new THREE.BoxGeometry(0.32, 0.012, 0.022),
    new THREE.MeshStandardMaterial({ color: 0x2a3550 }),
  )
  arm.rotation.y = Math.PI / 4
  group.add(arm)
  const arm2 = arm.clone()
  arm2.rotation.y = -Math.PI / 4
  group.add(arm2)

  const props = []
  const propGeo = new THREE.CircleGeometry(0.06, 24)
  const propMat = new THREE.MeshStandardMaterial({
    color: 0x9ec5ff,
    transparent: true,
    opacity: 0.55,
    side: THREE.DoubleSide,
  })
  const offsets = [
    [0.12, 0.12],
    [-0.12, 0.12],
    [0.12, -0.12],
    [-0.12, -0.12],
  ]
  for (const [px, pz] of offsets) {
    const prop = new THREE.Mesh(propGeo, propMat)
    prop.rotation.x = -Math.PI / 2
    prop.position.set(px, 0.03, pz)
    group.add(prop)
    props.push(prop)
  }

  // A small "nose" marker so the heading is visible in the third-person view.
  const nose = new THREE.Mesh(
    new THREE.BoxGeometry(0.03, 0.03, 0.06),
    new THREE.MeshStandardMaterial({ color: 0xff6b6b, emissive: 0x551111 }),
  )
  nose.position.set(0, 0, -0.1)
  group.add(nose)

  group.userData.props = props
  return group
}

function makeObstacle(obstacle) {
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(obstacle.w * S, obstacle.h * S, obstacle.d * S),
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(obstacle.color || '#8a8a8a'),
      roughness: 0.85,
      metalness: 0.05,
    }),
  )
  // Obstacle base sits on the floor; center is at half-height.
  const center = simToThree(obstacle.x, obstacle.y, obstacle.h / 2)
  mesh.position.copy(center)
  mesh.castShadow = true
  mesh.receiveShadow = true
  return mesh
}

export function createWorld(layout) {
  const room = layout.room
  const scene = new THREE.Scene()
  scene.background = new THREE.Color(0x0b1020)
  scene.fog = new THREE.Fog(0x0b1020, 6, 16)

  // Lighting.
  scene.add(new THREE.HemisphereLight(0xbcd0ff, 0x202838, 1.0))
  const dir = new THREE.DirectionalLight(0xffffff, 1.1)
  dir.position.set(3, 6, 4)
  scene.add(dir)

  // Floor.
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(room.width * S, room.depth * S),
    new THREE.MeshStandardMaterial({ color: 0x141b2e, roughness: 0.95 }),
  )
  floor.rotation.x = -Math.PI / 2
  scene.add(floor)

  const grid = new THREE.GridHelper(
    Math.max(room.width, room.depth) * S,
    Math.round(Math.max(room.width, room.depth) / 25),
    0x2a3550,
    0x1c2640,
  )
  grid.position.y = 0.001
  scene.add(grid)

  // Walls (opaque so the drone camera has something to see).
  const wallMat = new THREE.MeshStandardMaterial({
    color: 0x223052,
    roughness: 0.9,
    side: THREE.DoubleSide,
  })
  const hw = (room.width * S) / 2
  const hd = (room.depth * S) / 2
  const h = room.height * S
  const wallDefs = [
    { w: room.width * S, pos: [0, h / 2, -hd] },
    { w: room.width * S, pos: [0, h / 2, hd], ry: Math.PI },
    { w: room.depth * S, pos: [-hw, h / 2, 0], ry: Math.PI / 2 },
    { w: room.depth * S, pos: [hw, h / 2, 0], ry: -Math.PI / 2 },
  ]
  for (const def of wallDefs) {
    const wall = new THREE.Mesh(new THREE.PlaneGeometry(def.w, h), wallMat)
    wall.position.set(...def.pos)
    if (def.ry) wall.rotation.y = def.ry
    scene.add(wall)
  }

  // Obstacles.
  const obstacles = []
  for (const obstacle of layout.obstacles || []) {
    const mesh = makeObstacle(obstacle)
    scene.add(mesh)
    obstacles.push(mesh)
  }

  // Drone + first-person camera.
  const drone = makeDrone()
  scene.add(drone)

  const fpvCamera = new THREE.PerspectiveCamera(72, 16 / 9, 0.05, 50)
  fpvCamera.position.set(0, 0.02, 0)
  fpvCamera.rotation.x = -0.12 // slight downward tilt, like a real drone cam
  drone.add(fpvCamera)

  const thirdPersonCamera = new THREE.PerspectiveCamera(
    55,
    window.innerWidth / window.innerHeight,
    0.05,
    100,
  )
  thirdPersonCamera.position.set(hw * 0.8, h * 1.3, hd * 1.7)

  return { scene, drone, obstacles, fpvCamera, thirdPersonCamera }
}
