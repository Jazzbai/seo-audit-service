import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { AppProviders } from './context/AppContext'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AppProviders>
        <a className="skip-link" href="#main-content">Skip to content</a>
        <App />
      </AppProviders>
    </BrowserRouter>
  </StrictMode>,
)
