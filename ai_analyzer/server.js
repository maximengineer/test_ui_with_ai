const express = require('express');
const { GoogleGenerativeAI } = require('@google/generative-ai');

const app = express();
const port = 3000;

// Middleware to parse JSON bodies. Increased limit for base64 images.
app.use(express.json({ limit: '50mb' }));

// Basic health check endpoint
app.get('/health', (req, res) => {
  res.status(200).send('OK');
});

// Validation function for structured analysis requests
function validateStructuredRequest(requestBody) {
  const errors = [];
  const { url, system_context, structured_data, screenshots, context_hints } = requestBody;
  
  if (!url || typeof url !== 'string') {
    errors.push('Missing or invalid URL field');
  }
  
  if (!system_context || typeof system_context !== 'string') {
    errors.push('Missing or invalid system_context field');
  }
  
  if (!structured_data || typeof structured_data !== 'object') {
    errors.push('Missing or invalid structured_data field');
  } else {
    // Validate structured_data has expected format
    const requiredKeys = ['html_changes', 'css_changes', 'js_changes'];
    for (const key of requiredKeys) {
      if (!structured_data[key]) {
        errors.push(`Missing ${key} in structured_data`);
      }
    }
  }
  
  if (!screenshots || typeof screenshots !== 'object') {
    errors.push('Missing or invalid screenshots field');
  } else {
    if (!screenshots.baseline && !screenshots.current) {
      errors.push('At least baseline or current screenshot must be provided');
    }
  }
  
  if (!context_hints || typeof context_hints !== 'object') {
    errors.push('Missing or invalid context_hints field');
  }
  
  return errors;
}

// Structured data analysis endpoint
app.post('/api/compare', async (req, res) => {
  const apiKey = process.env.GEMINI_API_KEY;

  if (!apiKey) {
    console.error('GEMINI_API_KEY environment variable not set.');
    return res.status(500).json({
      overall_severity: 'ERROR',
      business_impact: 'HIGH',
      detailed_analysis: {
        visual_changes: [],
        functional_impact: ['Server configuration error - AI analysis unavailable'],
        technical_correlation: []
      },
      recommendations: {
        immediate_actions: ['Configure GEMINI_API_KEY environment variable'],
        review_items: [],
        acceptance_criteria: 'Fix server configuration before analysis can proceed'
      },
      confidence_score: 1.0,
      reasoning: 'Server configuration error'
    });
  }

  // Handle structured data analysis (only format needed)
  return handleStructuredAnalysis(req, res, apiKey);
});

// Structured data analysis handler
async function handleStructuredAnalysis(req, res, apiKey) {
  const { url, system_context, structured_data, screenshots, context_hints } = req.body;
  
  // Enhanced validation of request data
  const validationErrors = validateStructuredRequest(req.body);
  if (validationErrors.length > 0) {
    return res.status(400).json({
      overall_severity: 'ERROR',
      business_impact: 'HIGH',
      detailed_analysis: {
        visual_changes: [],
        functional_impact: validationErrors,
        technical_correlation: []
      },
      recommendations: {
        immediate_actions: ['Fix request data format', 'Provide all required fields'],
        review_items: validationErrors,
        acceptance_criteria: 'Request must include valid structured data and screenshots'
      },
      confidence_score: 1.0,
      reasoning: 'Invalid request format',
      validation_errors: validationErrors
    });
  }

  try {
    const genAI = new GoogleGenerativeAI(apiKey);
    const modelName = process.env.GEMINI_MODEL || 'gemini-2.5-flash';
    const model = genAI.getGenerativeModel({ model: modelName });

    // Create enhanced prompt with structured data context
    const prompt = `
${system_context}

ANALYSIS TASK:
Analyze the following web UI regression data for: ${url}

STRUCTURED DATA PROVIDED:
${JSON.stringify({
  change_summary: structured_data.change_summary,
  html_changes_summary: {
    total_changes: structured_data.html_changes?.summary?.total_changes || 0,
    structural_changes: structured_data.html_changes?.summary?.structural_changes || 0,
    content_changes: structured_data.html_changes?.summary?.content_changes || 0
  },
  css_changes: {
    changes_detected: structured_data.css_changes?.changes_detected || false,
    total_changes: structured_data.css_changes?.summary?.total_changes || 0
  },
  js_changes: {
    changes_detected: structured_data.js_changes?.changes_detected || false,
    total_changes: structured_data.js_changes?.summary?.total_changes || 0
  }
}, null, 2)}

SPECIFIC CODE CHANGES:
${structured_data.html_changes?.changes ? 
  structured_data.html_changes.changes.slice(0, 5).map(change => 
    `- ${change.type}: ${change.description}${change.code_snippet ? '\n  Code: ' + change.code_snippet.substring(0, 200) + '...' : ''}`
  ).join('\n') : 'No specific code changes provided'}

CONTEXT HINTS:
- Total HTML changes: ${context_hints?.total_html_changes || 0}
- CSS changes detected: ${context_hints?.css_changes_detected || false}  
- JS changes detected: ${context_hints?.js_changes_detected || false}
- Change severity: ${context_hints?.change_severity || 'unknown'}
- Has visual differences: ${context_hints?.has_visual_differences || false}

Please analyze the screenshots along with this structured data and provide your assessment in the required JSON format.
    `;

    // Prepare image parts
    const imageParts = [];
    if (screenshots.baseline) {
      imageParts.push({ inlineData: { data: screenshots.baseline, mimeType: 'image/png' } });
    }
    if (screenshots.current) {
      imageParts.push({ inlineData: { data: screenshots.current, mimeType: 'image/png' } });
    }
    if (screenshots.visual_diff) {
      imageParts.push({ inlineData: { data: screenshots.visual_diff, mimeType: 'image/png' } });
    }

    const result = await model.generateContent([prompt, ...imageParts]);
    const responseText = result.response.text();

    // Clean the response to ensure it's valid JSON
    const cleanedJsonString = responseText.replace(/```json/g, '').replace(/```/g, '').trim();
    
    let jsonResponse;
    try {
      jsonResponse = JSON.parse(cleanedJsonString);
      
      // Validate response has required fields
      if (!jsonResponse.overall_severity || !jsonResponse.business_impact) {
        throw new Error('AI response missing required fields');
      }
      
    } catch (parseError) {
      console.error('Failed to parse AI response:', parseError.message);
      console.error('Raw response:', cleanedJsonString);
      
      // Fallback structured response when AI returns invalid JSON
      return res.status(500).json({
        overall_severity: 'ERROR',
        business_impact: 'HIGH',
        detailed_analysis: {
          visual_changes: ['Unable to parse AI analysis response'],
          functional_impact: ['AI service returned malformed data'],
          technical_correlation: []
        },
        recommendations: {
          immediate_actions: ['Check AI service configuration', 'Review prompt format'],
          review_items: ['AI response format'],
          acceptance_criteria: 'AI service must return valid JSON format'
        },
        confidence_score: 0.0,
        reasoning: 'AI response parsing failed',
        error_details: {
          parse_error: parseError.message,
          raw_response_preview: cleanedJsonString.substring(0, 200) + '...'
        }
      });
    }

    // Add metadata to response
    jsonResponse.analysis_metadata = {
      request_type: 'enhanced_structured_analysis',
      data_sources: ['screenshots', 'html_changes', 'css_changes', 'js_changes'],
      total_changes_analyzed: context_hints?.total_html_changes || 0,
      timestamp: new Date().toISOString()
    };

    res.status(200).json(jsonResponse);

  } catch (error) {
    console.error('Error in enhanced analysis:', error);
    res.status(500).json({
      overall_severity: 'ERROR',
      business_impact: 'HIGH',
      detailed_analysis: {
        visual_changes: [],
        functional_impact: [`Analysis failed: ${error.message}`],
        technical_correlation: []
      },
      recommendations: {
        immediate_actions: ['Check AI analyzer service logs', 'Retry analysis'],
        review_items: [],
        acceptance_criteria: 'Resolve service error before proceeding'
      },
      confidence_score: 0.0,
      reasoning: `Service error: ${error.message}`
    });
  }
}

app.listen(port, () => {
  console.log(`Gemini AI service listening on port ${port}`);
});