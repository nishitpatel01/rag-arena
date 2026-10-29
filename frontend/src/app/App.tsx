import React from "react";
import { useRoutes } from "react-router-dom";
import { AppDataProvider } from "./AppDataProvider";
import { BattleSessionsProvider } from "./BattleSessions";
import { IngestSessionProvider } from "./IngestSessions";
import { SafeModeProvider } from "./SafeMode";
import { Shell } from "./Shell";
import { routes } from "./routes";
import { isMockRequested } from "../features/levels/useBattleStream";

export const App: React.FC = () => {
  const element = useRoutes(routes);
  return (
    <AppDataProvider>
      <SafeModeProvider>
        <IngestSessionProvider>
          <BattleSessionsProvider>
            <Shell mockBanner={isMockRequested()}>{element}</Shell>
          </BattleSessionsProvider>
        </IngestSessionProvider>
      </SafeModeProvider>
    </AppDataProvider>
  );
};

export default App;
