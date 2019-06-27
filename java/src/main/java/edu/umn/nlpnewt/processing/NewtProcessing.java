/*
 * Copyright 2019 Regents of the University of Minnesota.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package edu.umn.nlpnewt.processing;

import edu.umn.nlpnewt.*;
import edu.umn.nlpnewt.services.NewtServices;
import edu.umn.nlpnewt.timing.NewtTiming;
import io.grpc.Server;
import io.grpc.internal.AbstractServerImplBuilder;
import io.grpc.netty.NettyServerBuilder;
import org.jetbrains.annotations.NotNull;

import java.net.InetSocketAddress;
import java.util.concurrent.Executors;

/**
 * Performs dependency injection of the different processing components.
 */
@Internal
public class NewtProcessing {
  private final ProcessorServerOptions options;
  private final EventsClient eventsClient;
  private final NewtServices newtServices;
  private final NewtTiming newtTiming;

  private String processorName = null;
  private String processorId = null;
  private ContextManager contextManager = null;
  private EventProcessor processor = null;
  private AbstractServerImplBuilder<?> serverImplBuilder = null;
  private Runner runner = null;
  private ProcessorService service = null;


  public NewtProcessing(
      ProcessorServerOptions options,
      EventsClient eventsClient,
      NewtServices newtServices,
      NewtTiming newtTiming
  ) {
    this.options = options;
    this.eventsClient = eventsClient;
    this.newtServices = newtServices;
    this.newtTiming = newtTiming;
  }

  public @NotNull String getProcessorName() {
    if (processorName == null) {
      processorName = options.getProcessor().getClass().getAnnotation(Processor.class).value();
    }
    return processorName;
  }

  public NewtProcessing setProcessorName(String processorName) {
    this.processorName = processorName;
    return this;
  }

  public String getProcessorId() {
    if (processorId == null) {
      processorId = options.getIdentifier();
      if (processorId == null) {
        processorId = getProcessorName();
      }
    }
    return processorId;
  }

  public NewtProcessing setProcessorId(String processorId) {
    this.processorId = processorId;
    return this;
  }

  public ContextManager getContextManager() {
    if (contextManager == null) {
      contextManager = new ContextManagerImpl(
          newtServices.getServiceLifecycle(),
          getProcessorId()
      );
    }
    return contextManager;
  }

  public NewtProcessing setContextManager(ContextManager contextManager) {
    this.contextManager = contextManager;
    return this;
  }

  public EventProcessor getProcessor() {
    if (processor == null) {
      EventProcessor processor = options.getProcessor();
      if (processor == null) {
        throw new IllegalStateException("Processor must be specified");
      }
      processor.setContext(getContextManager().getContext());
      this.processor = processor;
    }
    return processor;
  }

  public NewtProcessing setProcessor(EventProcessor processor) {
    this.processor = processor;
    return this;
  }

  public Runner getRunner() {
    if (runner == null) {
      runner = new RunnerImpl(
          getProcessor(),
          eventsClient,
          getContextManager(),
          getProcessorName(),
          getProcessorId()
      );
    }
    return runner;
  }

  public NewtProcessing setRunner(Runner runner) {
    this.runner = runner;
    return this;
  }

  public ProcessorService getService() {
    if (service == null) {
      service = new ProcessorServiceImpl(
          newtServices.getServiceLifecycle(),
          getRunner(),
          newtTiming.getTimesCollector(Executors.newSingleThreadExecutor()),
          options.getUniqueServiceId(),
          options.getRegister()
      );
    }
    return service;
  }

  public NewtProcessing setService(ProcessorService service) {
    this.service = service;
    return this;
  }

  public AbstractServerImplBuilder<?> getServerImplBuilder() {
    if (serverImplBuilder == null) {
      InetSocketAddress socketAddress = new InetSocketAddress(options.getAddress(), options.getPort());
      serverImplBuilder = NettyServerBuilder.forAddress(socketAddress);
    }
    return serverImplBuilder;
  }

  public NewtProcessing setServerImplBuilder(AbstractServerImplBuilder<?> serverImplBuilder) {
    this.serverImplBuilder = serverImplBuilder;
    return this;
  }

  public ProcessorServer getProcessorServer() {
    Server server = getServerImplBuilder()
        .addService(getService())
        .addService(newtServices.getHealthStatusManager().getHealthService())
        .build();
    return new ProcessorServer(options.getAddress(), server, getService());
  }
}