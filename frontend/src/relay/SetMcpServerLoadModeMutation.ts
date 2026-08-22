import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {SetMcpServerLoadModeMutation} from '../__generated__/SetMcpServerLoadModeMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SetMcpServerLoadModeMutation($name: String!, $mode: String!) {
    setMcpServerLoadMode(name: $name, mode: $mode) {
      name
      loadMode
      toolCount
      tools
    }
  }
`;

export function commitSetMcpServerLoadMode(name: string, mode: 'always' | 'lazy') {
  return new Promise<void>((resolve, reject) => {
    commitMutation<SetMcpServerLoadModeMutation>(environment, {
      mutation,
      variables: {name, mode},
      onCompleted: (_res, errors) => (errors?.length ? reject(new Error(errors[0].message)) : resolve()),
      onError: reject,
    });
  });
}
