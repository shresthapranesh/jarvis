import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {StartMcpLoginMutation} from '../__generated__/StartMcpLoginMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation StartMcpLoginMutation($name: String!, $origin: String!) {
    startMcpLogin(name: $name, origin: $origin)
  }
`;

/** Where to send the browser to sign in to `name`; it comes back to this origin. */
export function commitStartMcpLogin(name: string) {
  return new Promise<string>((resolve, reject) => {
    commitMutation<StartMcpLoginMutation>(environment, {
      mutation,
      variables: {name, origin: window.location.origin},
      onCompleted: (res, errors) => {
        if (errors && errors.length) { reject(new Error(errors.map(e => e.message).join('; '))); return; }
        resolve(res.startMcpLogin);
      },
      onError: reject,
    });
  });
}
