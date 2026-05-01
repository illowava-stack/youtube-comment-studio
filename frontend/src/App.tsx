import { useState, useEffect, useRef } from 'react';
import './App.css';

interface CapturedImage {
  filename: string;
  url: string;
  summary: string;
  padded: boolean;
}

interface ProgressEvent {
  type: 'log' | 'error' | 'complete';
  message: string;
  progress?: number;
  images?: CapturedImage[];
}

function App() {
  const [url, setUrl] = useState('');
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('geminiApiKey') || '');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState<'idle' | 'running' | 'completed' | 'error'>('idle');
  const [logs, setLogs] = useState<string[]>([]);
  const [progress, setProgress] = useState(0);
  const [images, setImages] = useState<CapturedImage[]>([]);
  const [skipCount, setSkipCount] = useState(0);
  const [currentBatchIds, setCurrentBatchIds] = useState<number[]>([]);
  const logsEndRef = useRef<HTMLDivElement>(null);

  const startExtraction = async (type: 'new' | 'more' | 'rescan' = 'new') => {
    if (!url) return;
    if (!apiKey) {
      setStatus('error');
      setLogs(['Error: Please provide a Gemini API Key.']);
      return;
    }
    
    // Save API key to local storage for convenience
    localStorage.setItem('geminiApiKey', apiKey);
    try {
      setStatus('running');
      let logMsg = 'Starting extraction...';
      if (type === 'more') logMsg = 'Extracting more comments...';
      if (type === 'rescan') logMsg = 'Re-scanning current batch for missed comments...';
      setLogs([logMsg]);
      setProgress(0);
      
      let currentSkip = skipCount;
      if (type === 'more') {
        currentSkip = skipCount + 20;
      } else if (type === 'new') {
        currentSkip = 0;
      }
      
      if (type === 'new') {
        setImages([]);
        setCurrentBatchIds([]);
      } else if (type === 'more') {
        setCurrentBatchIds([]);
      }
      
      setSkipCount(currentSkip);
      
      const response = await fetch('/api/extract', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ 
          url, 
          api_key: apiKey,
          skip: currentSkip, 
          is_rescan: type === 'rescan', 
          exclude_ids: type === 'rescan' ? currentBatchIds : [] 
        }),
      });
      
      const data = await response.json();
      setTaskId(data.task_id);
    } catch (error) {
      setStatus('error');
      setLogs((prev) => [...prev, `Failed to start: ${error}`]);
    }
  };

  useEffect(() => {
    if (!taskId || status !== 'running') return;

    const eventSource = new EventSource(`/api/progress/${taskId}`);
    
    eventSource.onmessage = (event) => {
      const data: ProgressEvent = JSON.parse(event.data);
      
      setLogs((prev) => [...prev, data.message]);
      if (data.progress !== undefined) {
        setProgress(data.progress);
      }
      
      if (data.type === 'complete') {
        setStatus('completed');
        if (data.images) {
          setImages(prev => [...prev, ...data.images]);
          const newIds = data.images.map(img => {
            const match = img.filename.match(/_(\d+)\.png$/);
            return match ? parseInt(match[1]) : -1;
          }).filter(id => id !== -1);
          setCurrentBatchIds(prev => [...prev, ...newIds]);
        }
        eventSource.close();
      } else if (data.type === 'error') {
        setStatus('error');
        eventSource.close();
      }
    };

    eventSource.onerror = () => {
      setStatus('error');
      setLogs((prev) => [...prev, 'Connection to server lost.']);
      eventSource.close();
    };

    return () => {
      eventSource.close();
    };
  }, [taskId, status]);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  return (
    <div className="app-container">
      <div className="glass-panel main-panel">
        <h1 className="title">YouTube Studio Comments</h1>
        <p className="subtitle">AI-powered high-quality comment screenshot extractor</p>
        
        <div className="input-group" style={{ flexDirection: 'column', gap: '10px' }}>
          <input 
            type="password" 
            placeholder="Enter your Gemini API Key..." 
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={status === 'running'}
            className="url-input"
            style={{ width: '100%' }}
          />
          <div style={{ display: 'flex', gap: '10px', width: '100%' }}>
            <input 
              type="text" 
              placeholder="Paste YouTube Video URL here..." 
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={status === 'running'}
              className="url-input"
              style={{ flex: 1 }}
            />
            <button 
              onClick={() => startExtraction('new')} 
              disabled={status === 'running' || !url || !apiKey}
              className="extract-btn"
            >
              {status === 'running' ? 'Extracting...' : 'Extract Comments'}
            </button>
          </div>
        </div>
        
        {(status === 'running' || status === 'completed' || status === 'error') && (
          <div className="progress-section">
            <div className="progress-bar-container">
              <div 
                className="progress-bar-fill" 
                style={{ width: `${progress}%` }}
              ></div>
            </div>
            <div className="progress-text">{Math.round(progress)}% Complete</div>
            
            <div className="logs-container">
              {logs.map((log, index) => (
                <div key={index} className="log-entry">
                  <span className="log-indicator">►</span> {log}
                </div>
              ))}
              <div ref={logsEndRef} />
            </div>
          </div>
        )}
      </div>

      {images.length > 0 && (
        <div className="glass-panel gallery-panel">
          <div className="gallery-header">
            <h2 className="gallery-title">Extracted High-Value Comments ({images.length})</h2>
            <div className="gallery-actions">
              <button 
                onClick={() => startExtraction('rescan')} 
                disabled={status === 'running'}
                className="extract-more-btn rescan-btn"
                style={{ marginRight: '10px', backgroundColor: '#4a3b8c' }}
              >
                현재 20개 다시 꼼꼼히 검토 (Re-scan)
              </button>
              <button 
                onClick={() => startExtraction('more')} 
                disabled={status === 'running'}
                className="extract-more-btn"
              >
                다음 20개 추출하기 (Next 20)
              </button>
            </div>
          </div>
          <div className="gallery-grid">
            {images.map((img, idx) => (
              <div key={idx} className="image-card">
                <div className="image-wrapper">
                  <img src={img.url} alt={img.summary} />
                </div>
                <div className="image-info">
                  <span className="summary-badge">{img.summary}</span>
                  <a href={img.url} download className="download-btn">
                    Download
                  </a>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
