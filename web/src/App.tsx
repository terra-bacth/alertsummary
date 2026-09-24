import { Header } from './components/Header';
import { Rail } from './components/Rail';
import { Stage } from './components/Stage';
import { useDashboard } from './hooks/useDashboard';

export default function App() {
  const dash = useDashboard();

  return (
    <div className="app">
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0 }}>
        <Header streamState={dash.streamState} igniting={dash.igniting} onIgnite={dash.ignite} />
        <div className="shell">
          <Rail
            overview={dash.overview}
            incidents={dash.incidents}
            selectedId={dash.selectedId}
            onSelect={dash.select}
          />
          <Stage incident={dash.selected} now={dash.now} />
        </div>
      </div>
    </div>
  );
}
