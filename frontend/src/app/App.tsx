import React from "react";
import { useRoutes } from "react-router-dom";
import { AppDataProvider } from "./AppDataProvider";
import { Shell } from "./Shell";
import { routes } from "./routes";
import { isMockRequested } from "../features/levels/useBattleStream";

export const App: React.FC = () => {
  const element = useRoutes(routes);
  return (
    <AppDataProvider>
      <Shell mockBanner={isMockRequested()}>{element}</Shell>
    </AppDataProvider>
  );
};

export default App;
