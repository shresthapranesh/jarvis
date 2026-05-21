import {commitMutation, graphql} from 'react-relay';

import type {CreateWorkflowMutation} from '../__generated__/CreateWorkflowMutation.graphql';
import type {Workflow} from '../lib/types';
import {environment} from './environment';
import {mapWorkflow} from './WorkflowListQuery';

const mutation = graphql`
  mutation CreateWorkflowMutation($input: WorkflowCreateInput!) {
    createWorkflow(input: $input) {
      id
      name
      description
      definition
      notifications
      createdAt
      updatedAt
    }
  }
`;

interface CreatePayload {
  name: string;
  description?: string | null;
  definition: string;
  notifications?: string | null;
}

export function commitCreateWorkflow(p: CreatePayload): Promise<Workflow> {
  return new Promise((resolve, reject) => {
    commitMutation<CreateWorkflowMutation>(environment, {
      mutation,
      variables: {
        input: {
          name: p.name,
          description: p.description ?? null,
          definition: p.definition,
          notifications: p.notifications ?? null,
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapWorkflow(response.createWorkflow));
      },
      onError: reject,
    });
  });
}
