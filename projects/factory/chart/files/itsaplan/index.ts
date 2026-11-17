import { app } from "./app";
import { startBackgroundJobs } from "./background";
import { factoryRoutes } from "./factory";

app.use(factoryRoutes).listen(Number(process.env.API_PORT ?? 3000));
startBackgroundJobs();
