/*
 * Copyright 2019 Regents of the University of Minnesota
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

import edu.umn.nlpnewt.common.JsonObject;
import edu.umn.nlpnewt.common.JsonObjectBuilder;
import edu.umn.nlpnewt.common.JsonObjectImpl;
import edu.umn.nlpnewt.model.Event;
import edu.umn.nlpnewt.model.EventBuilder;
import org.jetbrains.annotations.NotNull;
import org.junit.jupiter.api.Test;

import java.time.Duration;

import static java.lang.Thread.sleep;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class EventProcessorTest {
  @Test
  void startedStopwatchNoContext() {
    WithStartedStopwatch withStartedStopwatch = new WithStartedStopwatch();
    withStartedStopwatch.process(EventBuilder.newBuilder().build(), JsonObjectImpl.newBuilder().build(), JsonObjectImpl.newBuilder());
    assertTrue(withStartedStopwatch.duration.toMillis() >= 1);
  }

  @Test
  void startedStopwatchWithContext() {
    try (ProcessorContext ignored = ProcessorBase.enterContext("processor")) {
      WithStartedStopwatch withStartedStopwatch = new WithStartedStopwatch();
      withStartedStopwatch.process(EventBuilder.newBuilder().build(), JsonObjectImpl.newBuilder().build(), JsonObjectImpl.newBuilder());
      assertTrue(withStartedStopwatch.duration.toMillis() >= 1);
    }
  }

  @Test
  void unstartedStopwatchNoContext() {
    WithUnstartedStopwatch withUnstartedStopwatch = new WithUnstartedStopwatch();
    withUnstartedStopwatch.process(EventBuilder.newBuilder().build(), JsonObjectImpl.newBuilder().build(), JsonObjectImpl.newBuilder());
    assertTrue(withUnstartedStopwatch.duration.toMillis() >= 20);
  }

  @Test
  void unstartedStopwatchWithContext() {
    try (ProcessorContext ignored = ProcessorBase.enterContext("processor")) {
      WithUnstartedStopwatch withUnstartedStopwatch = new WithUnstartedStopwatch();
      withUnstartedStopwatch.process(EventBuilder.newBuilder().build(), JsonObjectImpl.newBuilder().build(), JsonObjectImpl.newBuilder());
      assertTrue(withUnstartedStopwatch.duration.toMillis() >= 20);
    }
  }

  private class WithStartedStopwatch extends EventProcessor {
    Duration duration;

    @Override
    public void process(
        @NotNull Event event,
        @NotNull JsonObject params,
        @NotNull JsonObjectBuilder result
    ) {
      Stopwatch stopwatch = startedStopwatch("foo");
      try {
        sleep(1);
      } catch (InterruptedException e) {
        e.printStackTrace();
      }
      stopwatch.stop();
      duration = stopwatch.elapsed();
    }
  }

  private class WithUnstartedStopwatch extends EventProcessor {
    private Duration duration;

    @Override
    public void process(
        @NotNull Event event,
        @NotNull JsonObject params,
        @NotNull JsonObjectBuilder result
    ) {
      Stopwatch stopwatch = unstartedStopwatch("foo");
      for (int i = 0; i < 20; i++) {
        stopwatch.start();
        try {
          sleep(1);
        } catch (InterruptedException e) {
          e.printStackTrace();
        }
        stopwatch.stop();
      }
      duration = stopwatch.elapsed();
    }
  }
}
