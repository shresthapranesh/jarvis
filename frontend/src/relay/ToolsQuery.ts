import {graphql} from 'react-relay';

export const toolsQuery = graphql`
  query ToolsQuery {
    tools {
      id
      key
      kind
      name
      description
      group
      enabled
      requiresApproval
      inPrompt
      available
      detail
    }
  }
`;
