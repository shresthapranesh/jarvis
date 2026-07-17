import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {AddMcpServerMutation} from '../__generated__/AddMcpServerMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation AddMcpServerMutation($name: String!, $configJson: String!) {
    addMcpServer(name: $name, configJson: $configJson) {
      name
      config
      transport
      command
      url
      toolCount
      enabled
    }
  }
`;

export function commitAddMcpServer(name: string, configJson: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<AddMcpServerMutation>(environment, {
      mutation,
      variables: {name, configJson},
      onCompleted: (_res, errors) => {
        if (errors && errors.length) { reject(new Error(errors.map(e => e.message).join('; '))); return; }
        resolve();
      },
      onError: reject,
    });
  });
}
