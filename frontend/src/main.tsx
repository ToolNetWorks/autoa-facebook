import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { App } from '@/app/App';
import { AuthProvider } from '@/app/AuthProvider';
import { ToastProvider } from '@/app/ToastProvider';
import '@/styles/tokens.css';
import '@/styles/base.css';
import '@/styles/components.css';
import '@/styles/layout.css';

const container = document.getElementById('root');
if (!container) throw new Error('Thiếu phần tử #root');

createRoot(container).render(
  <StrictMode>
    <HashRouter>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </HashRouter>
  </StrictMode>,
);
