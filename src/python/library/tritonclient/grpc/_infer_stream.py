#!/usr/bin/env python3

# Copyright 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import queue
import threading

import grpc
from tritonclient.utils import *

from ._infer_result import InferResult
from ._utils import get_error_grpc, raise_error


class _InferStream:
    """Supports sending inference requests and receiving corresponding
    requests on a gRPC bi-directional stream.

    Parameters
    ----------
    callback : function
        Python function that is invoked upon receiving response from
        the underlying stream. The function must reserve the last two
        arguments (result, error) to hold InferResult and
        InferenceServerException objects respectively which will be
        provided to the function when executing the callback. The
        ownership of these objects will be given to the user. The
        'error' would be None for a successful inference.
    """

    def __init__(self, callback, verbose):
        self._callback = callback
        self._verbose = verbose
        self._request_queue = queue.Queue()
        self._handler = None
        self._active = True

    def __del__(self):
        self.close()

    def close(self):
        """Gracefully close underlying gRPC streams. Note that this call
        blocks till response of all currently enqueued requests are not
        received.
        """
        if self._handler is not None:
            self._request_queue.put(None)
            if self._handler.is_alive():
                self._handler.join()
                if self._verbose:
                    print("stream stopped...")
            self._handler = None

    def _init_handler(self, response_iterator):
        """Initializes the handler to process the response from
        stream and execute the callbacks.

        Parameters
        ----------
        response_iterator : iterator
            The iterator over the gRPC response stream.

        """
        if self._handler is not None:
            raise_error("Attempted to initialize already initialized InferStream")
        # Create a new thread to handle the gRPC response stream
        self._handler = threading.Thread(
            target=self._process_response, args=(response_iterator,)
        )
        self._handler.start()
        if self._verbose:
            print("stream started...")

    def _enqueue_request(self, request):
        """Enqueues the specified request object to be provided
        in gRPC request stream.

        Parameters
        ----------
        request : ModelInferRequest
            The protobuf message holding the ModelInferRequest

        """
        if self._active:
            self._request_queue.put(request)
        else:
            raise_error(
                "The stream is no longer in valid state, the error detail "
                "is reported through provided callback. A new stream should "
                "be started after stopping the current stream."
            )

    def _get_request(self):
        """Returns the request details in the order they were added.
        The call to this function will block until the requests
        are available in the queue. InferStream._enqueue_request
        adds the request to the queue.

        Returns
        -------
        protobuf message
            The ModelInferRequest protobuf message.

        """
        request = self._request_queue.get()
        return request

    def _process_response(self, responses):
        """Worker thread function to iterate through the response stream and
        executes the provided callbacks.

        Parameters
        ----------
        responses : iterator
            The iterator to the response from the server for the
            requests in the stream.

        """
        try:
            for response in responses:
                if self._verbose:
                    print(response)
                result = error = None
                if response.error_message != "":
                    error = InferenceServerException(msg=response.error_message)
                else:
                    result = InferResult(response.infer_response)
                self._callback(result=result, error=error)
        except grpc.RpcError as rpc_error:
            # On GRPC error, refresh the active state to indicate if the stream
            # can still be used. The stream won't be closed here as the thread
            # executing this function is managed by stream and may cause
            # circular wait
            self._active = responses.is_active()
            error = get_error_grpc(rpc_error)
            self._callback(result=None, error=error)


class _RequestIterator:
    """An iterator class to provide data to gRPC request stream.

    Parameters
    ----------
    stream : InferStream
        The InferStream that holds the context to an active stream.

    """

    def __init__(self, stream):
        self._stream = stream

    def __iter__(self):
        return self

    def __next__(self):
        request = self._stream._get_request()
        if request is None:
            raise StopIteration

        return request