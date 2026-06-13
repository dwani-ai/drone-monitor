import { useEffect, useMemo, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'

function formatValue(value, fallback = 'unknown') {
  if (value === undefined || value === null || value === '') return fallback
  return String(value)
}

function eventTitle(event) {
  if (event.type === 'tool_call') return `Tool call: ${event.command || event.name}`
  if (event.type === 'tool_result') return `Tool result: ${event.command || event.name}`
  if (event.type === 'gemini_text') return 'Gemini text'
  if (event.type === 'session') return 'Gemini Live'
  return event.type || 'Event'
}

function eventBody(event) {
  if (event.type === 'tool_result' && event.result?.summary) {
    return event.result.summary
  }
  if (event.type === 'gemini_text') return event.text
  if (event.message) return event.message
  return JSON.stringify(event.result || event.args || event, null, 2)
}

function App() {
  const [health, setHealth] = useState(null)
  const [droneStatus, setDroneStatus] = useState(null)
  const [events, setEvents] = useState([])
  const [lastCommand, setLastCommand] = useState(null)
  const [streamKey, setStreamKey] = useState(0)
  const [pollDrone, setPollDrone] = useState(false)

  const latestGeminiResponse = useMemo(() => {
    return events.find((event) => event.type === 'tool_result' && event.result?.summary)
  }, [events])

  async function loadHealth() {
    const response = await fetch(`${API_BASE}/api/health`)
    setHealth(await response.json())
  }

  async function loadStatus() {
    const response = await fetch(`${API_BASE}/api/drone/status`)
    const data = await response.json()
    setDroneStatus(data)
    return data
  }

  async function sendDroneCommand(command) {
    setLastCommand({ command, status: 'running' })
    const response = await fetch(`${API_BASE}/api/drone/${command}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    const data = await response.json()
    setLastCommand({ command, status: data.status, message: data.message })
    setDroneStatus(data)
    if (['connect', 'takeoff', 'status'].includes(command)) {
      setPollDrone(true)
    }
    if (['land', 'shutdown'].includes(command)) {
      setPollDrone(false)
    }
    if (['connect', 'takeoff', 'snapshot'].includes(command)) {
      setStreamKey((value) => value + 1)
    }
  }

  useEffect(() => {
    loadHealth().catch((error) =>
      setHealth({ status: 'error', message: error.message }),
    )

    fetch(`${API_BASE}/api/events/recent`)
      .then((response) => response.json())
      .then((data) => setEvents((data.events || []).slice(-80).reverse()))
      .catch(() => setEvents([]))

    const eventSource = new EventSource(`${API_BASE}/events`)
    eventSource.onmessage = (message) => {
      const event = JSON.parse(message.data)
      setEvents((current) => [event, ...current].slice(0, 80))
    }
    eventSource.onerror = () => {
      setEvents((current) => [
        {
          type: 'dashboard',
          message: 'Waiting for Gemini Live event stream...',
          timestamp: new Date().toISOString(),
        },
        ...current,
      ].slice(0, 80))
    }

    return () => eventSource.close()
  }, [])

  useEffect(() => {
    if (!pollDrone) return undefined

    loadStatus().catch((error) =>
      setDroneStatus({ status: 'error', message: error.message }),
    )
    const intervalId = window.setInterval(() => {
      loadStatus().catch((error) =>
        setDroneStatus({ status: 'error', message: error.message }),
      )
    }, 2500)

    return () => window.clearInterval(intervalId)
  }, [pollDrone])

  const drone = droneStatus?.drone || {}
  const serviceStatus = health?.drone_service?.status || 'unknown'

  return (
    <main className="dashboard">
      <header className="hero">
        <div>
          <p className="eyebrow">Gemini Live Drone Monitor</p>
          <h1>Live drone view and assistant responses</h1>
        </div>
        <div className={`status-pill ${serviceStatus}`}>
          Drone service: {serviceStatus}
        </div>
      </header>

      <section className="grid">
        <div className="panel video-panel">
          <div className="panel-header">
            <div>
              <h2>Drone Camera</h2>
              <p>MJPEG stream from `drone_service.py`</p>
            </div>
            <button onClick={() => setStreamKey((value) => value + 1)}>
              Refresh stream
            </button>
          </div>
          <div className="video-frame">
            <img
              key={streamKey}
              src={`${API_BASE}/api/drone/stream`}
              alt="Live drone camera stream"
            />
          </div>
        </div>

        <aside className="panel">
          <h2>Flight Controls</h2>
          <div className="controls">
            <button onClick={() => sendDroneCommand('connect')}>Connect</button>
            <button onClick={() => sendDroneCommand('takeoff')}>Take off</button>
            <button onClick={() => sendDroneCommand('status')}>Status</button>
            <button onClick={() => sendDroneCommand('snapshot')}>Snapshot</button>
            <button className="danger" onClick={() => sendDroneCommand('land')}>
              Land
            </button>
          </div>
          {lastCommand && (
            <p className="command-result">
              Last command: {lastCommand.command} ({lastCommand.status})
              {lastCommand.message ? ` - ${lastCommand.message}` : ''}
            </p>
          )}
          <div className="telemetry">
            <div>
              <span>Battery</span>
              <strong>{formatValue(drone.battery_percent, '--')}%</strong>
            </div>
            <div>
              <span>Height</span>
              <strong>{formatValue(drone.height_cm, '--')} cm</strong>
            </div>
            <div>
              <span>Airborne</span>
              <strong>{formatValue(drone.airborne, 'false')}</strong>
            </div>
            <div>
              <span>Stream</span>
              <strong>{formatValue(drone.stream_started, 'false')}</strong>
            </div>
          </div>
        </aside>
      </section>

      <section className="grid lower-grid">
        <div className="panel response-panel">
          <h2>Latest Gemini Response</h2>
          <p>
            {latestGeminiResponse
              ? latestGeminiResponse.result.summary
              : 'Ask Gemini Live: "what do you see?" The vision summary will appear here.'}
          </p>
        </div>

        <div className="panel events-panel">
          <h2>Gemini Live Events</h2>
          <div className="events">
            {events.length === 0 && (
              <p className="muted">
                No Gemini Live events yet. Start `gemini_live_computer_use.py`.
              </p>
            )}
            {events.map((event, index) => (
              <article key={`${event.timestamp || 'event'}-${index}`} className="event">
                <div>
                  <strong>{eventTitle(event)}</strong>
                  <time>{event.timestamp || ''}</time>
                </div>
                <pre>{eventBody(event)}</pre>
              </article>
            ))}
          </div>
        </div>
      </section>
    </main>
  )
}

export default App
