package com.example.svc.controller;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.svc.service.impl.ClientOnboardingServiceImpl;

@RestController
public class EnrollmentController {

    @Autowired
    private ClientOnboardingServiceImpl onboardingService;

    @PostMapping("/v1/enroll")
    public String enroll(ClientOnboardingServiceImpl.OnboardConfig cfg)
            throws Exception {
        return onboardingService.enrollDevice(cfg);
    }

    @GetMapping("/health")
    public String health() {
        return "ok";
    }
}
