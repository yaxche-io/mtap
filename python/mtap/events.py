# Copyright 2019 Regents of the University of Minnesota.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Events service client API and wrapper classes.

Attributes:
    GenericLabelAdapter (ProtoLabelAdapter): label adapter used for standard (non-distinct)
        :obj:`~mtap.GenericLabel`.
    GenericLabelAdapter (ProtoLabelAdapter): label adapter used for distinct (non-overlapping)
        :obj:`~mtap.GenericLabel`.
"""

import collections
import threading
import uuid
from abc import abstractmethod, ABC
from enum import Enum
from typing import Iterator, List, Dict, MutableMapping, Generic, TypeVar, NamedTuple, \
    ContextManager, Iterable, Optional, Sequence, Union

import grpc
from grpc_health.v1 import health_pb2_grpc, health_pb2

from mtap import _discovery
from mtap import _structs
from mtap._config import Config
from mtap.api.v1 import events_pb2_grpc, events_pb2
from mtap.constants import EVENTS_SERVICE_NAME
from mtap.label_indices import label_index, LabelIndex
from mtap.labels import GenericLabel, Label

__all__ = [
    'Event',
    'LabelIndexType',
    'LabelIndexInfo',
    'Document',
    'Labeler',
    'ProtoLabelAdapter',
    'EventsClient',
    'GenericLabelAdapter',
    'DistinctGenericLabelAdapter'
]

L = TypeVar('L', bound=Label)


class Event:
    """An object for interacting with a specific event locally or on the events service.

    The Event object functions as a map from string document names to :obj:`Document` objects that
    can be used to access document data from the events server.

    Keyword Args:
        event_id (~typing.Optional[str]):
            A globally-unique identifier for the event, or omit / none for a random UUID.
        client (~typing.Optional[EventsClient):
            A client for an events service to push any changes to the event to.
        only_create_new (bool): Fails if the event already exists on the events service.

    Examples:
        >>> with EventsClient() as client, Event(event_id='id', client=client) as event:
        >>>     # use event
        >>>     ...
    """

    def __init__(self, *, event_id: Optional[str] = None, client: Optional['EventsClient'] = None,
                 only_create_new: bool = False):
        self._event_id = event_id or str(uuid.uuid4())
        self._client = client
        self._lock = threading.RLock()
        if client is not None:
            client.open_event(self._event_id, only_create_new=only_create_new)

    @property
    def client(self) -> Optional['EventsClient']:
        return self._client

    @property
    def event_id(self) -> str:
        """str: The globally unique identifier for this event."""
        return self._event_id

    @property
    def documents(self) -> MutableMapping[str, 'Document']:
        """~typing.MutableMapping[str, Document]: A mutable mapping of strings to :obj:`Document`
        objects that can be used to query and add documents to the event."""
        try:
            return self._documents
        except AttributeError:
            self._documents = _Documents(self, self.client)
            return self._documents

    @property
    def metadata(self) -> MutableMapping[str, str]:
        """~typing.MutableMapping[str, str]: A mutable mapping of strings to strings that can be
        used to query and add metadata to the event."""
        try:
            return self._metadata
        except AttributeError:
            self._metadata = _Metadata(self, self.client)
            return self._metadata

    @property
    def binaries(self) -> MutableMapping[str, bytes]:
        """~typing.MutableMapping[str, str]: A mutable mapping of strings to bytes that can be used
        to query and add binary data to the event."""
        try:
            return self._binaries
        except AttributeError:
            self._binaries = _Binaries(self, self.client)
            return self._binaries

    @property
    def created_indices(self) -> Dict[str, List[str]]:
        """~typing.Dict[str, ~typing.List[str]]: A mapping of document names to a list of the names
        of all the label indices that have been added to that document"""
        return {document_name: document.created_indices
                for document_name, document in self.documents.items()}

    def close(self):
        """Closes this event. Lets the event service know that we are done with the event,
        allowing to clean up the event if no other clients have open leases to it."""
        if self.client is not None:
            self.release_lease()

    def create_document(self, document_name: str, text: str) -> 'Document':
        """Adds a document to the event keyed by `document_name` and
        containing the specified `text`.

        Args:
            document_name (str): The event-unique identifier for the document, example: 'plaintext'.
            text (str):
                The content of the document. This is a required field, document text is final and
                immutable, as changing the text would very likely invalidate any labels on the
                document.

        Returns:
            Document: The added document.
        """
        if not isinstance(text, str):
            raise ValueError('text is not string.')
        document = Document(document_name, text=text, event=self)
        self.documents[document_name] = document
        return document

    def add_document(self, document: 'Document'):
        """Adds the document to this event, first uploading to events service if this event has a
        client connection to the events service.

        Args:
            document (Document): The document to add to this event.
        """
        document._event = self
        document._client = self.client
        document._event_id = self.event_id
        self.documents[document.document_name] = document

    def __enter__(self) -> 'Event':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def add_created_indices(self, created_indices):
        for k, v in created_indices.items():
            try:
                doc = self._documents[k]
                doc.add_created_indices(v)
            except KeyError:
                pass

    def lease(self):
        if self.client is not None:
            self.client.open_event(self.event_id, only_create_new=False)

    def release_lease(self):
        if self.client is not None:
            self.client.close_event(self.event_id)


class LabelIndexType(Enum):
    """The type of serialized labels contained in the label index."""
    UNKNOWN = 0
    """Label index not set or type not known."""

    JSON = 1
    """JSON / Generic Label index"""

    OTHER = 2
    """Other / custom protobuf label index"""


LabelIndexType.UNKNOWN.__doc__ = """Label index not set or type not known."""
LabelIndexType.JSON.__doc__ = """JSON / Generic Label index"""
LabelIndexType.OTHER.__doc__ = """Other / custom protobuf label index"""

LabelIndexInfo = NamedTuple('LabelIndexInfo',
                            [('index_name', str),
                             ('type', LabelIndexType)])
LabelIndexInfo.__doc__ = """Information about a label index contained on a document."""
LabelIndexInfo.index_name.__doc__ = """str: The name of the label index."""
LabelIndexInfo.type.__doc__ = """LabelIndexType: The type of the label index."""


class Document:
    """An object for interacting with text and labels stored on an :class:`Event`.

    Documents are keyed by their name, and pipelines can store different pieces of
    related text on a single processing event using multiple documents. An example would be storing
    the text of one language on one document, and a translation on another, or storing the
    rtf or html encoding on one document, and the parsed plaintext on another document.

    Both the document text and any added label indices are immutable. This is to enable
    parallelization and distribution of processing, and to prevent changes to the dependency graph
    of label indices and text, which can make debugging difficult.

    Args:
        document_name (str): The document name identifier.

    Keyword Args:
        text (~typing.Optional[str]):
            The document text, can be omitted if this is an existing document and text needs to be
            retrieved from the events service.
        event (~typing.Optional[Event]):
            The parent event of this document. If the event has a client, then that client will be
            used to share changes to this document with all other clients of the Events service. In
            that case, text should only be specified if it is the known existing text of the
            document.

    Examples:
        Local document:

        >>> document = Document('plaintext', text='Some document text.')

        Existing distributed object:

        >>> with EventsClient(address='localhost:8080') as client, \\
        >>>      Event(event_id='1', client=client) as event:
        >>>     document = event.documents['plaintext']
        >>>     document.text
        'Some document text fetched from the server.'

        New distributed object:

        >>> with EventsClient(address='localhost:8080') as client, \\
        >>>      Event(event_id='1', client=client) as event:
        >>>     document = Document('plaintext', text='Some document text.')
        >>>     event.add_document(document)

        or

        >>> with EventsClient(address='localhost:8080') as client, \\
        >>>      Event(event_id='1', client=client) as event:
        >>>     document = event.create_document('plaintext', text='Some document text.')

    """

    def __init__(self, document_name: str, *, text: Optional[str] = None, event: Optional[Event] = None):
        if not isinstance(document_name, str):
            raise TypeError('Document name is not string.')
        self._document_name = document_name
        self._event = event
        self._client = None
        self._event_id = None
        if event is not None:
            self._client = event.client
            self._event_id = event.event_id
        self._text = text
        self._label_indices = {}
        self._labelers = []
        self._created_indices = []
        if self._client is None and text is None:
            raise ValueError('Document without text or an event with a client to fetch the text '
                             'from.')

    @property
    def event(self) -> Event:
        """Event: The parent event of this document."""
        return self._event

    @property
    def document_name(self) -> str:
        """str: The unique identifier for this document on the event."""
        return self._document_name

    @property
    def text(self):
        """str: The document text."""
        if self._text is None and self._client is not None:
            self._text = self._client.get_document_text(self._event_id, self._document_name)
        return self._text

    @property
    def created_indices(self) -> List[str]:
        """~typing.List[str]: A list of all of the label index names that have created on this
                document using a labeler either locally or by remote pipeline components invoked on
                this document."""
        return list(self._created_indices)

    def get_label_indices_info(self) -> List[LabelIndexInfo]:
        """The list of label index information objects.

        Returns:
            ~typing.List[LabelIndexInfo]:
                A list of objects containing information about all label indices on this document.
        """
        if self._client is not None:
            return self._client.get_label_index_info(self._event_id, self._document_name)
        return [LabelIndexInfo(k, LabelIndexType.JSON) for k, v in self._label_indices.items()]

    def get_label_index(
            self,
            label_index_name: str,
            *, label_adapter: Optional['ProtoLabelAdapter[L]'] = None
    ) -> LabelIndex[Union[GenericLabel, L]]:
        """Gets the document's label index with the specified key.

        Will fetch from the events service if it is not cached locally if the document has an event
        with a client. Uses the `label_adapter` argument to perform unmarshalling from the proto
        message if specified.

        Args:
            label_index_name (str): The name of the label index to get.

        Keyword Args:
            label_adapter (~typing.Optional[ProtoLabelAdapter]):
                The label adapter for the target type. If omitted :obj:`GenericLabel` will be used.

        Returns:
            LabelIndex: The requested label index.
        """

        if label_index_name in self._label_indices:
            return self._label_indices[label_index_name]
        if label_adapter is None:
            label_adapter = GenericLabelAdapter
        if self._client is not None:
            index = self._client.get_labels(self._event_id, self._document_name,
                                            label_index_name,
                                            adapter=label_adapter)
            for label in index:
                label.document = self
            self._label_indices[label_index_name] = index
            return index
        else:
            raise KeyError('Document does not have label index:', label_index_name)

    def get_labeler(self,
                    label_index_name: str,
                    *,
                    distinct: Optional[bool] = None,
                    label_adapter: Optional['ProtoLabelAdapter[L]'] = None) -> 'Labeler':
        """Creates a function that can be used to add labels to a label index.

        Args:
            label_index_name (str): A document-unique identifier for the label index to be created.

        Keyword Args:
            distinct (~typing.Optional[bool]):
                Optional, if using generic labels, whether to use distinct generic labels or
                non-distinct generic labels, will default to False.
            label_adapter (~typing.Optional[ProtoLabelAdapter[L]]):
                The label adapter to use to perform marshalling of objects to proto messages.

        Returns:
            Labeler: A callable when used in conjunction with the 'with' keyword will automatically
            handle uploading any added labels to the server.

        Examples:
            >>> with document.get_labeler('sentences', distinct=True) as labeler:
            >>>     labeler(0, 25, sentence_type='STANDARD')
            >>>     sentence = labeler(26, 34)
            >>>     sentence.sentence_type = 'FRAGMENT'

        """
        if label_index_name in self._labelers:
            raise KeyError("Labeler already in use: " + label_index_name)
        if label_adapter is not None and distinct is not None:
            raise ValueError("Either 'distinct' or 'label_type_id' can be set, but not both.")
        if label_adapter is None:
            if distinct is None:
                distinct = False
            label_adapter = (DistinctGenericLabelAdapter if distinct else GenericLabelAdapter)

        labeler = Labeler(self._client, self, label_index_name, label_adapter)
        self._labelers.append(label_index_name)
        return labeler

    def add_labels(
            self,
            label_index_name: str,
            labels: Sequence[Union['Label', L]],
            *,
            distinct: Optional[bool] = None,
            label_adapter: Optional['ProtoLabelAdapter[L]'] = None
    ) -> LabelIndex[Union[GenericLabel, L]]:
        """Skips using a labeler and adds the sequence of labels as a new label index.

        Args:
            label_index_name (str): The name of the label index.
            labels (~typing.Sequence[Label]): The labels to add.

        Keyword Args:
            distinct (~typing.Optional[bool]):
                Whether the index is distinct or non-distinct.
            label_adapter (~typing.Optional[ProtoLabelAdapter[L]]):
                The label adapter to use to perform marshalling of objects to proto messages.

        Returns:
            LabelIndex: The new label index created from the labels.
        """
        if label_index_name in self._label_indices:
            raise KeyError("Label index already exists with name: " + label_index_name)
        if distinct is not None and label_adapter is not None:
            raise ValueError("Arguments 'distinct' and 'label_adapter' are mutually exclusive.")
        if distinct is None:
            distinct = False
        if label_adapter is None:
            label_adapter = (DistinctGenericLabelAdapter if distinct else GenericLabelAdapter)

        labels = sorted(labels, key=lambda l: l.location)
        for label in labels:
            label.document = self
        if self._client is not None:
            self._client.add_labels(event_id=self.event.event_id,
                                    document_name=self.document_name,
                                    index_name=label_index_name,
                                    labels=labels,
                                    adapter=label_adapter)
        self._created_indices.append(label_index_name)
        index = label_adapter.create_index(labels)
        self._label_indices[label_index_name] = index
        return index

    def add_created_indices(self, created_indices: Iterable[str]):
        # Internal, used by the pipeline to add any indices created remotely to the
        # "created_indices" on a local document.
        return self._created_indices.extend(created_indices)


class Labeler(Generic[L], ContextManager['Labeler']):
    """Object provided by :func:`~mtap.Document.get_labeler` which is responsible for adding labels
    to a label index on a document.

    Args:
        client (EventsClient): Client to upload labels to events service when done.
        document (Document): The parent document labels are being added to.
        label_index_name (str): The label index name key.
        label_adapter (ProtoLabelAdapter):
            The label adapter to perform marshalling from objects to proto messages.
    """

    def __init__(self,
                 client: 'EventsClient',
                 document: Document,
                 label_index_name: str,
                 label_adapter: 'ProtoLabelAdapter[L]'):
        self._client = client
        self._document = document
        self._label_index_name = label_index_name
        self._label_adapter = label_adapter
        self.is_done = False
        self._current_labels = []
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs) -> L:
        """Calls the constructor for the label type adding it to the list of labels to be uploaded.

        Args:
            args: Arguments passed to the label type's constructor.
            kwargs: Keyword arguments passed to the label type's constructor.

        Returns:
            Label: The object that was created by the label type's constructor.

        Examples:
            >>> labeler(0, 25, some_field='some_value', x=3)
            GenericLabel(start_index=0, end_index=25, some_field='some_value', x=3)
        """
        label = self._label_adapter.create_label(*args, document=self._document, **kwargs)
        self._current_labels.append(label)
        return label

    def __enter__(self) -> 'Labeler':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            return False
        self.done()

    def done(self):
        """Finalizes the label index, uploads the added labels to the events service.

        Normally called automatically on exit from a context manager block, but can be manually
        invoked if the labeler is not used in a context manager block."""
        with self._lock:
            if self.is_done:
                return
            self.is_done = True
            self._document.add_labels(self._label_index_name, self._current_labels,
                                      label_adapter=self._label_adapter)


class ProtoLabelAdapter(ABC, Generic[L]):
    """Responsible for marshalling and unmarshalling of label objects to and from proto messages.
    """

    @abstractmethod
    def create_label(self, *args, **kwargs) -> L:
        """Called by labelers to create labels.

        Should include the positional arguments `start_index` and `end_index`, because those are
        required properties of labels.

        Args:
            args: Arbitrary args used to create the label.
            kwargs: Arbitrary keyword args used to create the label.

        Returns:
            Label: An object of the label type.
        """
        ...

    @abstractmethod
    def create_index_from_response(self, response: events_pb2.GetLabelsResponse) -> LabelIndex[L]:
        """Creates a LabelIndex from the response from an events service.

        Args:
            response (mtap.api.v1.events_pb2.GetLabelsResponse): The response protobuf message from
                the events service.

        Returns:
            LabelIndex[L]: A label index containing all the labels from the events service.
        """
        ...

    @abstractmethod
    def create_index(self, labels: Iterable[L]):
        """Creates a LabelIndex from an iterable of label objects.

        Args:
            labels (~typing.Iterable[L]): Labels to put in index.

        Returns:
            LabelIndex[L]: A label index containing all of the labels in the list.
        """
        ...

    @abstractmethod
    def add_to_message(self, labels: List[L], request: events_pb2.AddLabelsRequest):
        """Adds a list of labels to a request to the event service to add labels.

        Args:
            labels (Iterable[L]): The list of labels that need to be sent to the server.
            request (mtap.api.v1.events_pb2.AddLabelsRequest): The request proto message to add the
                labels to.
        """
        ...


class EventsClient:
    """A client object for interacting with the events service.

    Normally, users shouldn't have to use any of the methods on this object, as they are invoked by
    the globally distributed object classes of :obj:`Event`, :obj:`Document`, and :obj:`Labeler`.

    Keyword Args:
        address (~typing.Optional[str]): The events service target e.g. 'localhost:9090' or
            omit/None to use service discovery.
        stub (~typing.Optional[mtap.api.v1.events_pb2_grpc.EventsStub]): An existing events service
            client gRPC stub to use.

    Examples:
        >>> with EventsClient(address='localhost:50000') as client, \\
        >>>      Event(event_id='1', client=client) as event:
        >>>     document = event.create_document(document_name='plaintext',
        >>>                                      text='The quick brown fox jumps over the lazy dog.')
    """

    def __init__(self,
                 *, address: Optional[str] = None,
                 stub: Optional[events_pb2_grpc.EventsStub] = None):
        if stub is None:
            if address is None:
                discovery = _discovery.Discovery(Config())
                address = discovery.discover_events_service('v1')

            channel = grpc.insecure_channel(address)

            health = health_pb2_grpc.HealthStub(channel)
            hcr = health.Check(health_pb2.HealthCheckRequest(service=EVENTS_SERVICE_NAME))
            if hcr.status != health_pb2.HealthCheckResponse.SERVING:
                raise ValueError('Failed to connect to events service. Status:')

            self._channel = channel
            self.stub = events_pb2_grpc.EventsStub(channel)
        else:
            self.stub = stub
        self._is_open = True

    def __enter__(self) -> 'EventsClient':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def ensure_open(self):
        if not self._is_open:
            raise ValueError("Client to events service is not open")

    def open_event(self, event_id: str, only_create_new: bool):
        request = events_pb2.OpenEventRequest(event_id=event_id, only_create_new=only_create_new)
        try:
            response = self.stub.OpenEvent(request)
            assert response is not None
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.ALREADY_EXISTS:
                raise ValueError("Event already exists")

    def close_event(self, event_id):
        request = events_pb2.CloseEventRequest(event_id=event_id)
        response = self.stub.CloseEvent(request)
        return response is not None

    def get_all_metadata(self, event_id):
        request = events_pb2.GetAllMetadataRequest(event_id=event_id)
        response = self.stub.GetAllMetadata(request)
        return response.metadata

    def add_metadata(self, event_id, key, value):
        request = events_pb2.AddMetadataRequest(event_id=event_id, key=key, value=value)
        response = self.stub.AddMetadata(request)
        return response is not None

    def get_all_binary_data_names(self, event_id: str) -> List[str]:
        request = events_pb2.GetAllBinaryDataNamesRequest(event_id=event_id)
        response = self.stub.GetAllBinaryDataNames(request)
        return list(response.binary_data_names)

    def add_binary_data(self, event_id: str, binary_data_name: str, binary_data: bytes):
        request = events_pb2.AddBinaryDataRequest(event_id=event_id,
                                                  binary_data_name=binary_data_name,
                                                  binary_data=binary_data)
        response = self.stub.AddBinaryData(request)
        return response is not None

    def get_binary_data(self, event_id: str, binary_data_name: str) -> bytes:
        request = events_pb2.GetBinaryDataRequest(event_id=event_id,
                                                  binary_data_name=binary_data_name)
        response = self.stub.GetBinaryData(request)
        return response.binary_data

    def get_all_document_names(self, event_id):
        request = events_pb2.GetAllDocumentNamesRequest(event_id=event_id)
        response = self.stub.GetAllDocumentNames(request)
        return list(response.document_names)

    def add_document(self, event_id, document_name, text):
        request = events_pb2.AddDocumentRequest(event_id=event_id,
                                                document_name=document_name,
                                                text=text)
        response = self.stub.AddDocument(request)
        return response is not None

    def get_document_text(self, event_id, document_name):
        request = events_pb2.GetDocumentTextRequest(event_id=event_id,
                                                    document_name=document_name)
        response = self.stub.GetDocumentText(request)
        return response.text

    def get_label_index_info(self, event_id: str, document_name: str) -> List[LabelIndexInfo]:
        request = events_pb2.GetLabelIndicesInfoRequest(event_id=event_id,
                                                        document_name=document_name)
        response = self.stub.GetLabelIndicesInfo(request)
        result = []
        for index in response.label_index_infos:
            if index.type == events_pb2.GetLabelIndicesInfoResponse.LabelIndexInfo.JSON:
                index_type = LabelIndexType.JSON
            elif index.type == events_pb2.GetLabelIndicesInfoResponse.LabelIndexInfo.OTHER:
                index_type = LabelIndexType.OTHER
            else:
                index_type = LabelIndexType.UNKNOWN
            result.append(LabelIndexInfo(index.index_name, index_type))
        return result

    def add_labels(self, event_id, document_name, index_name, labels, adapter):
        request = events_pb2.AddLabelsRequest(event_id=event_id, document_name=document_name,
                                              index_name=index_name,
                                              no_key_validation=True)
        adapter.add_to_message(labels, request)
        response = self.stub.AddLabels(request)
        return response is not None

    def get_labels(self, event_id, document_name, index_name, adapter):
        request = events_pb2.GetLabelsRequest(event_id=event_id,
                                              document_name=document_name,
                                              index_name=index_name)
        response = self.stub.GetLabels(request)
        return adapter.create_index_from_response(response)

    def close(self):
        self._is_open = False
        try:
            self._channel.close()
        except AttributeError:
            pass


class _Documents(MutableMapping[str, Document]):
    def __init__(self, event: Event, client: Optional[EventsClient]):
        self.event = event
        self.event_id = event.event_id
        self.client = client
        self.documents = {}

    def __contains__(self, document_name: str) -> bool:
        if not isinstance(document_name, str):
            return False
        if document_name in self.documents:
            return True
        self._refresh_documents()
        return document_name in self.documents

    def __getitem__(self, document_name) -> 'Document':
        if not isinstance(document_name, str):
            raise KeyError
        try:
            return self.documents[document_name]
        except KeyError:
            pass
        self._refresh_documents()
        return self.documents[document_name]

    def __len__(self) -> int:
        self._refresh_documents()
        return len(self.documents)

    def __iter__(self) -> Iterator[str]:
        self._refresh_documents()
        return iter(self.documents)

    def __setitem__(self, k: str, v: Document) -> None:
        if self.client is not None:
            if not self.client.add_document(self.event_id, k, v.text):
                raise ValueError()
        v._event = self.event
        v._event_id = self.event_id
        v._client = self.client
        self.documents[k] = v

    def __delitem__(self, v: str) -> None:
        raise NotImplementedError()

    def _refresh_documents(self):
        if self.client is not None:
            document_names = self.client.get_all_document_names(self.event_id)
            for name in document_names:
                if name not in self.documents:
                    document = Document(name, event=self.event)
                    self.documents[name] = document


class _Metadata(MutableMapping[str, str]):
    def __init__(self, event: Event, client: Optional[EventsClient] = None):
        self._client = client
        self._event = event
        self._event_id = event.event_id
        self._metadata = {}

    def __contains__(self, key):
        if key in self._metadata:
            return True
        self._refresh_metadata()
        return key in self._metadata

    def __setitem__(self, key, value):
        if key in self:
            raise KeyError("Metadata already exists with key: " + key)
        self._metadata[key] = value
        if self._client is not None:
            if not self._client.add_metadata(self._event_id, key, value):
                raise ValueError()

    def __getitem__(self, key):
        try:
            return self._metadata[key]
        except KeyError:
            self._refresh_metadata()
        return self._metadata[key]

    def __delitem__(self, v) -> None:
        raise NotImplementedError

    def __iter__(self) -> Iterator[str]:
        self._refresh_metadata()
        return iter(self._metadata)

    def __len__(self) -> int:
        self._refresh_metadata()
        return len(self._metadata)

    def _refresh_metadata(self):
        if self._client is not None:
            response = self._client.get_all_metadata(self._event_id)
            self._metadata.update(response)


class _Binaries(collections.abc.MutableMapping):
    def __init__(self, event: Event, client: Optional[EventsClient] = None):
        self._client = client
        self._event = event
        self._event_id = event.event_id
        self._names = set()
        self._binaries = {}

    def __contains__(self, key):
        if key in self._names:
            return True
        self._refresh_binaries()
        return key in self._names

    def __setitem__(self, key, value):
        if key in self:
            raise KeyError("Binary already exists with name: " + key)
        self._names.add(key)
        self._binaries[key] = value
        if self._client is not None:
            if not self._client.add_binary_data(self._event_id, key, value):
                raise ValueError()

    def __getitem__(self, key):
        try:
            return self._binaries[key]
        except KeyError:
            pass
        if self._client is not None:
            b = self._client.get_binary_data(event_id=self._event_id, binary_data_name=key)
            self._names.add(b)
            self._binaries[key] = b
        return self._binaries[key]

    def __delitem__(self, v) -> None:
        raise NotImplementedError

    def __iter__(self) -> Iterator[str]:
        self._refresh_binaries()
        return iter(self._names)

    def __len__(self) -> int:
        self._refresh_binaries()
        return len(self._names)

    def _refresh_binaries(self):
        if self._client is not None:
            response = self._client.get_all_binary_data_names(self._event_id)
            self._names.update(response)


class _GenericLabelAdapter(ProtoLabelAdapter):
    def __init__(self, distinct):
        self.distinct = distinct

    def create_label(self, *args, **kwargs):
        return GenericLabel(*args, **kwargs)

    def create_index(self, labels: List[L]):
        return label_index(labels, self.distinct)

    def create_index_from_response(self, response):
        json_labels = response.json_labels
        labels = []
        for label in json_labels.labels:
            d = {}
            _structs.copy_struct_to_dict(label, d)
            generic_label = GenericLabel(**d)
            labels.append(generic_label)

        return label_index(labels, json_labels.is_distinct)

    def add_to_message(self, labels, request):
        json_labels = request.json_labels
        for label in labels:
            _structs.copy_dict_to_struct(label.fields, json_labels.labels.add(), [label])


GenericLabelAdapter = _GenericLabelAdapter(False)

DistinctGenericLabelAdapter = _GenericLabelAdapter(True)
