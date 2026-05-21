import {createClient} from 'graphql-ws';
import {
  Environment,
  type FetchFunction,
  Network,
  Observable,
  RecordSource,
  Store,
  type SubscribeFunction,
} from 'relay-runtime';

const httpUrl = '/graphql';

const wsUrl =
  typeof window === 'undefined'
    ? 'ws://localhost:8000/graphql'
    : `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/graphql`;

const fetchFn: FetchFunction = async (params, variables) => {
  const res = await fetch(httpUrl, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      accept: 'application/json',
    },
    body: JSON.stringify({
      query: params.text,
      operationName: params.name,
      variables,
    }),
  });
  return await res.json();
};

const wsClient = createClient({url: wsUrl});

const subscribeFn: SubscribeFunction = (operation, variables) => {
  return Observable.create((sink) => {
    if (!operation.text) {
      sink.error(new Error('operation text required for subscription'));
      return;
    }
    const dispose = wsClient.subscribe(
      {
        operationName: operation.name,
        query: operation.text,
        variables: variables as Record<string, unknown>,
      },
      {
        next: (data) => sink.next(data as never),
        error: (err) => sink.error(err as Error),
        complete: () => sink.complete(),
      },
    );
    return () => dispose();
  });
};

export const environment = new Environment({
  network: Network.create(fetchFn, subscribeFn),
  store: new Store(new RecordSource()),
});
