import { useEffect, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '@/app/AuthProvider';
import { Button } from '@/components/ui/Button';

interface AppShellProps {
  nav: { heading: string; items: { id: string; label: string }[] }[];
  activeId: string;
  onNavigate: (id: string) => void;
  title: ReactNode;
  breadcrumb?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  backTo?: { label: string; to: string };
}

/** The frame around every page: a sidebar, a top bar and the content area. */
export function AppShell({ nav, activeId, onNavigate, title, breadcrumb, actions, children, backTo }: AppShellProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const { session, signOut } = useAuth();
  const location = useLocation();

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname, location.hash]);

  return (
    <div className="app-shell">
      {menuOpen ? <div className="scrim" onClick={() => setMenuOpen(false)} /> : null}
      <aside className={menuOpen ? 'sidebar open' : 'sidebar'}>
        <div className="brand">
          <span className="brand-mark">T</span>
          <span className="brand-text">
            <b>ToolNet Publisher</b>
            <span>Facebook automation</span>
          </span>
        </div>
        <nav className="nav">
          {nav.map((section) => (
            <div className="nav-section" key={section.heading}>
              <div className="nav-heading">{section.heading}</div>
              {section.items.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  data-page={item.id}
                  className={item.id === activeId ? 'nav-item active' : 'nav-item'}
                  aria-current={item.id === activeId ? 'page' : undefined}
                  onClick={() => onNavigate(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          {session ? (
            <div className="row-between">
              <span className="truncate">{session.username}</span>
              <Button size="sm" variant="ghost" onClick={() => void signOut()}>
                Đăng xuất
              </Button>
            </div>
          ) : null}
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <button
            type="button"
            className="icon-btn menu-toggle"
            aria-label="Mở menu"
            onClick={() => setMenuOpen((open) => !open)}
          >
            ☰
          </button>
          {backTo ? (
            <Link className="btn sm" to={backTo.to}>
              {backTo.label}
            </Link>
          ) : null}
          {breadcrumb ? <div className="crumb truncate">{breadcrumb}</div> : null}
          <h1 className="truncate">{title}</h1>
          {actions ? <div className="topbar-actions">{actions}</div> : null}
        </header>
        <main className="page">{children}</main>
      </div>
    </div>
  );
}
