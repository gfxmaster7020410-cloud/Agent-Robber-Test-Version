import { OrchModel } from './types';

export function routeRequest(models: OrchModel[], requirements: any): OrchModel {
  const candidates = models
    .filter(m => m.isActive)
    .sort((a, b) => {
      const scoreA = (a.routing.priority * 10) + (a.status === 'HEALTHY' ? 5 : 0);
      const scoreB = (b.routing.priority * 10) + (b.status === 'HEALTHY' ? 5 : 0);
      return scoreB - scoreA;
    });
  return candidates[0];
}
