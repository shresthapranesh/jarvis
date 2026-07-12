import {graphql} from 'react-relay';

// Refetchable pagination fragment for the messages connection on a Conversation.
// Lives in its own file because Relay requires fragment names to match the
// module they're declared in.
export const conversationPageFragment = graphql`
  fragment ConversationPageFragment on Conversation
    @refetchable(queryName: "ConversationPageRefetchQuery")
    @argumentDefinitions(
      count: {type: "Int", defaultValue: 10}
      cursor: {type: "String"}
    )
  {
    id
    title
    model
    createdAt
    messages(last: $count, before: $cursor)
      @connection(key: "ConversationPageFragment_messages") {
      edges {
        node {
          id
          role
          content
          model
          status
          inputTokens
          outputTokens
          createdAt
          steps {
            id
            node
            source
            data
            seq
            createdAt
          }
        }
      }
      pageInfo {
        hasPreviousPage
        startCursor
      }
    }
  }
`;
